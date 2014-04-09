# coding=utf-8
__author__ = 'Gareth Coles'

import copy
import logging
import os
import sys
import webassets

from beaker.middleware import SessionMiddleware

from bottle import default_app, request, hook, abort, static_file, redirect, \
    run, route
from bottle import mako_template as template

from twisted.internet.error import ReactorAlreadyRunning

from .events import ServerStartedEvent, ServerStoppedEvent, ServerStoppingEvent
from .yielder import Yielder

from system.command_manager import CommandManager
from system.decorators import run_async_daemon
from system.event_manager import EventManager
from system.events.general import ReactorStartedEvent
from system.plugin import PluginObject
from system.protocols.generic.user import User
from system.storage.manager import StorageManager
from system.storage.formats import YAML, JSON
from utils.packages import packages
from utils.password import mkpasswd
from utils.misc import AttrDict


class BottlePlugin(PluginObject):

    app = None
    host = "127.0.0.1"
    port = 8080
    output_requests = True
    session_opts = {}
    _secret = ""

    api_routes_list = []
    navbar_items = {}
    additional_headers = []

    config = None
    commands = None
    data = None
    env = None

    packs = None
    events = None
    storage = None

    # region Internal

    def setup(self):
        self.logger.debug("Entered setup method.")

        self.packs = packages.Packages()
        self.storage = StorageManager()

        try:
            self.config = self.storage.get_file(self, "config", YAML,
                                                "plugins/web.yml")
        except Exception:
            self.logger.exception("Error loading configuration!")
            self.logger.warn("Using the default configuration --> "
                             "http://127.0.0.1:8080")
        else:
            if not self.config.exists:
                self.logger.error("Unable to find config/plugins/web.yml")
                self.logger.warn("Using the default configuration --> "
                                 "http://127.0.0.1:8080")
            else:
                self.host = self.config["hostname"]
                self.port = self.config["port"]
                self.output_requests = self.config["output_requests"]

        try:
            self.data = self.storage.get_file(self, "data", JSON,
                                              "plugins/web/data.json")
        except Exception:
            self.logger.exception("Error loading data!")
            self.logger.error("This data file is required. Shutting down...")
            self._disable_self()
            return

        base_path = "web/static"

        if "secret" not in self.data:
            self.logger.warn("Generating secret. DO NOT SHARE IT WITH ANYONE!")
            self.logger.warn("It's stored in data/plugins/web/data.json - "
                             "keep this file secure!")
            with self.data:
                self.data["secret"] = mkpasswd(60, 20, 20, 20)

        self._secret = self.data["secret"]

        self.logger.info("Compiling and minifying Javascript and CSS..")
        self.env = webassets.Environment(base_path, "/static")
        self.env.debug = "--debug" in sys.argv
        if "--debug" in sys.argv:
            self.logger.warn("Environment is in debug mode, no compilation "
                             "will be done.")

        css = []
        js = []

        assets = os.listdir(base_path)
        for asset in assets:
            if asset.endswith(".css"):
                css.append(asset)
            elif asset.endswith(".js"):
                js.append(asset)

        if js:
            self.logger.info("Optimizing and compiling %s JavaScript files."
                             % len(js))
            js_bundle = webassets.Bundle(*js, filters="rjsmin",
                                         output="generated/packed.js")

            self.env.register("js", js_bundle)

            for url in self.env["js"].urls():
                self._add_javascript(url)
        else:
            self.logger.info("No JavaScript files were found. Nothing to do.")

        if css:
            self.logger.info("Optimizing and compiling %s CSS files."
                             % len(css))
            css_bundle = webassets.Bundle(*css, filters=["cssrewrite",
                                                         "cssmin"],
                                          output="generated/packed.css")

            self.env.register("css", css_bundle)

            for url in self.env["css"].urls():
                self._add_stylesheet(url)
        else:
            self.logger.info("No CSS files were found. Nothing to do.")

        self.session_opts = {
            "session.cookie_expires": True,
            "data_dir": "data/plugins/web/beaker/data",
            "lock_dir": "data/plugins/web/beaker/lock",
            "type": "file",
            "key": "Ultros",
            "secret": self._secret
        }

        self.app = default_app()
        self.app = SessionMiddleware(self.app, self.session_opts)
        self.events = EventManager()
        self.commands = CommandManager()

        def log_all():
            ip = request.remote_addr
            method = request.method
            fullpath = request.fullpath

            level = logging.INFO if self.output_requests else logging.DEBUG

            self.logger.log(level, "[%s] %s %s" % (ip, method, fullpath))

        hook("before_request")(log_all)

        self.events.add_callback("ReactorStarted", self,
                                 self.start_callback,
                                 0)

    def deactivate(self):
        if self.app:
            event = ServerStoppingEvent(self, self.app)
            self.events.run_callback("Web/ServerStopping", event)

            self.app.close()
            del self.app
            self.app = None

            event = ServerStoppedEvent(self)
            self.events.run_callback("Web/ServerStopped", event)
        super(PluginObject, self).deactivate()

    def start_callback(self, event=ReactorStartedEvent):
        self.logger.info("Starting Bottle app..")
        if self._start_bottle():
            self.add_route("/static/<path:path>", ["GET", "POST"], self.static)
            self.add_route("/static/", ["GET", "POST"], self.static_403)
            self.add_route("/static", ["GET", "POST"], self.static_403)

            self.add_route("/", ["GET", "POST"], self.index)
            self.add_route("/index", ["GET", "POST"], self.index)
            self.add_route("/login", ["GET"], self.login)
            self.add_route("/login", ["POST"], self.login_post)
            self.add_route("/logout", ["GET", "POST"], self.logout)

            self.add_navbar_entry("Home", "/")

    @run_async_daemon
    def _start_bottle(self):
        try:
            try:
                run(app=self.app, host=self.host, port=self.port,
                    server='twisted', quiet=True)
            except ReactorAlreadyRunning:
                self.logger.debug("Caught ReactorAlreadyRunning error.")

            self.logger.debug("Throwing event..")
            event = ServerStartedEvent(self, self.app)
            self.events.run_callback("Web/ServerStartedEvent", event)
            return True
        except Exception:
            self.logger.exception("Exception while running the Bottle app!")
            return False

    def _log_request(self, rq, level=logging.DEBUG):
        ip = rq.remote_addr
        self.logger.log("[%s] %s %s" % (ip, request.method, request.fullpath),
                        level)

    def _add_stylesheet(self, path):
        header = '<link rel="stylesheet" href="%s" />' % path
        self.add_header(header)

    def _add_javascript(self, path):
        header = '<script src="%s"></script>' % path
        self.add_header(header)

    # endregion

    # region Public API functions

    def add_navbar_entry(self, title, url):
        if title in self.navbar_items:
            return False
        self.logger.debug("Adding navbar entry: %s -> %s" % (title, url))
        self.navbar_items[title] = {"url": url, "active": False}
        return True

    def add_header(self, header):
        self.logger.debug("Adding header: %s" % header)
        self.additional_headers.append(header)

    def add_route(self, path, methods, *args, **kwargs):
        if not isinstance(methods, list):
            methods = [methods]
        _id = "%s|%s" % (path, ", ".join(methods))
        if _id in self.api_routes_list:
            return False
        self.api_routes_list.append(_id)

        self.logger.debug("Adding route: %s" % path)
        route(path, methods, *args, **kwargs)

        return True

    def get_yielder(self):
        return Yielder()

    def get_session(self, r=None):
        if r is None:
            r = self.get_objects()
        return r.session

    def get_objects(self):
        return AttrDict(
            request=request,
            abort=abort,
            hook=hook,
            static_file=static_file,
            template=template,
            session=request.environ.get("beaker.session")
        )

    def get_authorization(self, r=None):
        s = self.get_session(r)

        return AttrDict(
            authorized=s.get("authorized", False),
            auth_name=s.get("auth_name", None)
        )

    def is_authorized(self, r=None):
        a = self.get_authorization(r)
        return a["authorized"]

    def check_permission(self, perm, r=None):
        auth = self.get_authorization(r)
        caller = User(auth["auth_name"], "web")

        caller.auth_name = auth["auth_name"]
        caller.authorized = auth["authorized"]

        return self.commands.perm_handler.check(perm, caller, "web", "web")

    def wrap_template(self, content, title, nav="Home", r=None,
                      tpl="web/templates/generic.html", **kwargs):
        auth = self.get_authorization(r)
        nav_items = copy.deepcopy(self.navbar_items)
        if nav in nav_items:
            nav_items[nav]["active"] = True
        return template(tpl,
                        nav_items=nav_items,
                        headers=self.additional_headers,
                        content=content,
                        title=title,
                        auth=auth,
                        **kwargs)

    def require_login(self, r=None):
        if r is None:
            r = self.get_objects()

        if not self.is_authorized(r):
            # Login form
            return False, redirect("/login", 307)
        # They're logged in
        return True, None

    # endregion

    # region Routes

    def index(self):
        auth = self.get_authorization()
        nav_items = copy.deepcopy(self.navbar_items)
        nav_items["Home"]["active"] = True
        return template("web/templates/index.html",
                        nav_items=nav_items,
                        headers=self.additional_headers,
                        packages=self.packs.get_installed_packages(),
                        plugins=self.factory_manager.loaded_plugins.values(),
                        factories=self.factory_manager.factories,
                        auth=auth)

    def login(self):
        auth = self.get_authorization()
        nav_items = copy.deepcopy(self.navbar_items)
        if not self.is_authorized():
            # Show login form
            return template("web/templates/login.html",
                            nav_items=nav_items,
                            headers=self.additional_headers,
                            auth=auth,
                            failed=False)
        return redirect("/", 307)

    def login_post(self):
        auth = self.get_authorization()
        if not self.is_authorized():
            s = self.get_session()

            username = request.forms.get("username")
            password = request.forms.get("password")

            result = False

            for handler in self.commands.auth_handlers:
                result = handler.check_login(username, password)
                if result:
                    break

            if result:
                s["authorized"] = True
                s["auth_name"] = username
                s.save()
            else:
                nav_items = copy.deepcopy(self.navbar_items)
                nav_items["Login"]["active"] = True
                return template("web/templates/login.html",
                                nav_items=nav_items,
                                headers=self.additional_headers,
                                auth=auth,
                                failed=True)
        return redirect("/", 303)

    def logout(self):
        if self.is_authorized():
            s = self.get_session()
            del s["authorized"]
            del s["auth_name"]
            s.save()
            s.delete()
        return redirect("/", 307)

    def static(self, path):
        return static_file(path, root="web/static")

    def static_403(self):
        abort(403, "You may not list the static files.")

    # endregion

    pass  # So the regions work in PyCharm
