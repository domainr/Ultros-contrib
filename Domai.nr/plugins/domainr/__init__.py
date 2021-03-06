import treq

from system.command_manager import CommandManager
from system.decorators.ratelimit import RateLimiter, RateLimitExceededError

import system.plugin as plugin


__author__ = 'Sean'


class DomainrPlugin(plugin.PluginObject):

    _commands = None

    def setup(self):
        ### Grab important shit
        self._commands = CommandManager()

        ### Load stuff
        self._load()

        ### Register commands
        self._commands.register_command("domainrsearch",
                                        self.search_cmd,
                                        self,
                                        "domainr.search",
                                        aliases=[
                                            "domainr"
                                        ], default=True)
        self._commands.register_command("domainrinfo",
                                        self.info_cmd,
                                        self,
                                        "domainr.info", default=True)

    def reload(self):
        self._load()
        return True

    def _load(self):
        self.api = Domainr()

    def _respond(self, target, msg):
        """
        Convenience function for responding to something with a prefix. Not
        only does this avoid confusion, but it also stops people being able to
        execute other bot commands in the case that we need to put any
        user-supplied data at the start of a message.
        """
        target.respond("Domainr: " + msg)

    def _msg(self, protocol, target, *args):
        """
        Sends messages different ways, depending on protocol can_flood setting.
        If can_flood, each arg (and item in arg, if arg is list) is sent on its
        own line.
        If not can_flood, all args and join with a space, and args that are a
        list are joined with comma-space.
        Example:
            Call:
                _msg(proto, target, "Foo:", ["bar", "baz", "quux"])
            Can flood output:
                Foo:
                bar
                baz
                quux
            Cannot flood output:
            Foo: bar, baz, quux
        :param protocol:
        :param target:
        :param args:
        :return:
        """
        if protocol.can_flood:
            for msg in args:
                if isinstance(msg, list):
                    for m in msg:
                        self._respond(target, m)
                else:
                    self._respond(target, msg)
        else:
            to_send = []
            for msg in args:
                if isinstance(msg, list):
                    msg = ", ".join(msg)
                to_send.append(msg)
            self._respond(target, " ".join(to_send))

    def search_cmd(self, protocol, caller, source, command, raw_args,
                   parsed_args):
        if len(raw_args) == 0:
            caller.respond("Usage: {CHARS}%s <query>" % command)
            return
        else:
            try:
                deferred = self.api.search(raw_args)
                deferred.addCallbacks(
                    lambda r: self._search_cmd_result(protocol,
                                                      caller,
                                                      source,
                                                      r),
                    lambda f: self._cmd_error(caller, f)
                )
            except RateLimitExceededError:
                caller.respond("Command on cooldown - try again later")

    def info_cmd(self, protocol, caller, source, command, raw_args,
                 parsed_args):
        if len(raw_args) == 0:
            caller.respond("Usage: {CHARS}%s <domain>" % command)
            return
        else:
            try:
                deferred = self.api.info(raw_args)
                deferred.addCallbacks(
                    lambda r: self._info_cmd_result(protocol,
                                                    caller,
                                                    source,
                                                    r),
                    lambda f: self._cmd_error(caller, f)
                )
            except RateLimitExceededError:
                caller.respond("Command on cooldown - try again later")

    def _search_cmd_result(self, protocol, caller, source, result):
        """
        Receives the API response for search
        """
        loud = self._commands.perm_handler.check("domainr.search.loud",
                                                 caller,
                                                 source,
                                                 protocol)
        target = None
        if loud:
            target = source
        else:
            target = caller
        try:
            msgs = []
            for res in result["results"]:
                self.logger.trace(res)
                msg = u"%s%s - %s" % (res["domain"],
                                      res["path"],
                                      res["availability"])
                msgs.append(msg)
            self._msg(protocol, target, msgs)
        except:
            self.logger.exception("Please tell the developer about this error")

    def _info_cmd_result(self, protocol, caller, source, result):
        """
        Receives the API response for info
        """
        loud = self._commands.perm_handler.check("domainr.info.loud",
                                                 caller,
                                                 source,
                                                 protocol)
        target = None
        if loud:
            target = source
        else:
            target = caller
        try:
            msgs = []
            msgs.append(u"Availability: %s" % result["availability"])
            if result["availability"] in (Domainr.AVAILABLE, Domainr.MAYBE):
                msgs.append(u"Register: %s" % result["register_url"])
            self._msg(protocol, target, msgs)
        except:
            self.logger.exception("Please tell the developer about this error")

    def _cmd_error(self, caller, failure):
        """
        :type failure: twisted.python.failure.Failure
        """
        # Some errors will be caused by user input
        if failure.check(DomainrError):
            self._respond(caller, failure.value.message)
        else:
            self.logger.debug("Error while fetching info",
                              exc_info=(
                                  failure.type,
                                  failure.value,
                                  failure.tb
                              ))
            caller.respond("There was an error while contacting Domainr - "
                           "please alert a bot admin or try again later")


class Domainr(object):
    """
    Basic Domainr API wrapper. Returns parsed JSON response.
    """

    # Availability responses
    AVAILABLE = "available"
    TAKEN = "taken"
    UNAVAILABLE = "unavailable"
    MAYBE = "maybe"
    TLD = "tld"

    API_URL = "https://domainr.com/api/json/"

    def _handle_response(self, response):
        deferred = response.json()
        deferred.addCallback(self._handle_content)
        return deferred

    def _handle_content(self, result):
        if "error" in result:
            raise DomainrError(**result["error"])
        elif "error_message" in result:
            # Apparently the API doesn't follow the docs...
            raise DomainrError(message=result["error_message"])
        else:
            return result

    # I'll have to play around to see what the best limit/buffer is, but it
    # should be ~60 per minute anyway.
    # Sod it, the rate limiting plugin (coming soon) can deal with
    # burst/slowdown - we'll jsut set this to 60 per 60.
    @RateLimiter(limit=60, buffer=10, time_period=60)
    def _make_request(self, method, payload):
        """
        Actually make the HTTP request.
        :rtype : twisted.internet.defer.Deferred
        """
        # Convert unicode strings to utf8-encoded bytestrings (treq doesn't
        # seem to be able to encode unicode strings properly)
        # Alters payload in-place, but this is a "private" method, and we don't
        # reuse is after this call anyway.
        payload["client_id"] = "Ultros-Domai.nr"
        for k, v in payload.iteritems():
            if isinstance(v, unicode):
                payload[k] = v.encode("utf8")
        deferred = treq.get(self.API_URL + method, params=payload)
        deferred.addCallback(self._handle_response)
        return deferred

    def search(self, query):
        """
        Search for domain suggestions for the given query.
        :rtype : twisted.internet.defer.Deferred
        """
        payload = {
            "q": query
        }
        return self._make_request("search", payload)

    def info(self, domain):
        """
        Get info for given domain.
        :rtype : twisted.internet.defer.Deferred
        """
        payload = {
            "q": domain
        }
        return self._make_request("info", payload)


class DomainrError(Exception):

    def __init__(self, message, status=None, *args, **kwargs):
        Exception.__init__(self, message)
        self.message = message
        self.status = status
        # These should be empty, unless domainr changes their API.
        self._args = args
        self._kwargs = kwargs

    def __str__(self):
        status_msg = ""
        if self.status is not None:
            status_msg = "[%s] " % self.status
        msg = "%s: %s%s" % (self.__class__, status_msg, self.message)
        if len(self._args) > 0:
            msg += " | Args: %s" % ", ".join(self._args)
        if len(self._kwargs) > 0:
            msg += " | KWArgs: %s" % ", ".join(
                "%s: %s" % (k, v) for k, v in self._kwargs.iteritems()
            )
        return msg
