__author__ = 'Gareth Coles'

import urllib
from wordnik import swagger
from wordnik.WordApi import WordApi
from wordnik.WordsApi import WordsApi

from system.command_manager import CommandManager

import system.plugin as plugin

from system.plugins.manager import PluginManager
from system.protocols.generic.user import User
from system.storage.formats import YAML
from system.storage.manager import StorageManager


class DictPlugin(plugin.PluginObject):

    api_key = ""
    api_client = None
    word_api = None
    words_api = None

    commands = None
    config = None
    plugman = None
    storage = None

    @property
    def urls(self):
        return self.plugman.get_plugin("URLs")

    def setup(self):
        self.storage = StorageManager()
        try:
            self.config = self.storage.get_file(self, "config", YAML,
                                                "plugins/wordnik.yml")
        except Exception:
            self.logger.exception("Unable to load the configuration!")
            return self._disable_self()
        if not self.config.exists:
            self.logger.error("Unable to find the configuration at "
                              "config/plugins/wordnik.yml - Did you fill "
                              "it out?")
            return self._disable_self()
        if "apikey" not in self.config or not self.config["apikey"]:
            self.logger.error("Unable to find an API key; did you fill out the"
                              " config?")
            return self._disable_self()

        self._load()
        self.config.add_callback(self._load)

        self.plugman = PluginManager()

        self.commands = CommandManager()

        self.commands.register_command("dict", self.dict_command,
                                       self, "wordnik.dict", default=True)
        self.commands.register_command("wotd", self.wotd_command,
                                       self, "wordnik.wotd", default=True)

    def _load(self):
        self.api_key = self.config["apikey"]
        self.api_client = swagger.ApiClient(self.api_key,
                                            "http://api.wordnik.com/v4")
        self.word_api = WordApi(self.api_client)
        self.words_api = WordsApi(self.api_client)

    def dict_command(self, protocol, caller, source, command, raw_args,
                     parsed_args):
        args = raw_args.split()  # Quick fix for new command handler signature
        if len(args) < 1:
            caller.respond("Usage: {CHAR}dict <word to look up>")
        else:
            try:
                definition = self.get_definition(args[0])
                if not definition:
                    if isinstance(source, User):
                        caller.respond("%s | No definition found." % args[0])
                    else:
                        source.respond("%s | No definition found." % args[0])
                    return
                word = definition.word
                text = definition.text

                wiktionary_url = "http://en.wiktionary.org/wiki/%s" \
                                 % urllib.quote_plus(word)

                short_url = self.urls.tinyurl(wiktionary_url)
            except Exception as e:
                self.logger.exception("Error looking up word: %s" % args[0])
                caller.respond("Error getting definition: %s" % e)
            else:

                # Necessary attribution as per the Wordnik TOS
                if isinstance(source, User):
                    caller.respond("%s | %s (%s) - Provided by Wiktionary via "
                                   "the Wordnik API" % (word, text, short_url))
                else:
                    source.respond("%s | %s (%s) - Provided by Wiktionary via "
                                   "the Wordnik API" % (word, text, short_url))

    def wotd_command(self, protocol, caller, source, command, raw_args,
                     parsed_args):
        args = raw_args.split()  # Quick fix for new command handler signature
        try:
            wotd = self.get_wotd()
            definition = self.get_definition(wotd)
            if not definition:
                if isinstance(source, User):
                    caller.respond("%s | No definition found." % wotd)
                else:
                    source.respond("%s | No definition found." % wotd)
                return
            word = definition.word
            text = definition.text

            wiktionary_url = "http://en.wiktionary.org/wiki/%s" \
                             % urllib.quote_plus(word)

            short_url = self.urls.tinyurl(wiktionary_url)
        except Exception as e:
            self.logger.exception("Error looking up word of the day.")
            caller.respond("Error getting definition: %s" % e)
        else:

            # Necessary attribution as per the Wordnik TOS
            if isinstance(source, User):
                caller.respond("%s | %s (%s) - Provided by Wiktionary via "
                               "the Wordnik API" % (word, text, short_url))
            else:
                source.respond("%s | %s (%s) - Provided by Wiktionary via "
                               "the Wordnik API" % (word, text, short_url))

    def get_definition(self, word):
        result = self.word_api.getDefinitions(word, limit=1,
                                              sourceDictionaries="wiktionary")
        self.logger.trace("Data: %s" % result)
        if result:
            return result[0]
        return None

    def get_wotd(self):
        result = self.words_api.getWordOfTheDay()
        self.logger.trace("Data: %s" % result)
        return result.word
