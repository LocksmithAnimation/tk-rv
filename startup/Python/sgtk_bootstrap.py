# Copyright (c) 2016 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

from __future__ import print_function

import logging
import html
import sys
import os
import json
import traceback
from six.moves.urllib.parse import urlparse

from PySide2 import QtCore
from PySide2.QtCore import QCoreApplication, QObject


import rv
import rv.rvtypes as rvt
import rv.commands as rvc
import rv.extra_commands as rve

BOOTSTRAPING_ENV_VAR = "RV_ENGINE_BOOTSTRAPING"


class ToolkitBootstrap(rvt.MinorMode):
    """
    An RV mode that will handle bootstrapping SGTK and starting
    up the tk-rv engine. The mode expects an installation of
    tk-core to be present in the same directory underneath an
    "sgtk_core" subdirectory. Alternatively, an environment variable
    "TK_CORE" can be set to an alternate installation of tk-core
    to override the default behavior.
    """

    def __init__(self):
        """
        Initializes the RV mode. An environment variable "TK_RV_MODE_NAME"
        will be set to the name of this mode. That variable can then be
        used by other logic to generate menu items in RV associated with
        this mode.
        """
        self.startup_time = rvc.theTime()

        super(ToolkitBootstrap, self).__init__()

        self._mode_name = "sgtk_bootstrap"
        self.init(
            self._mode_name,
            [
                ("external-gma-play-entity", self.pre_process_event, ""),
                ("external-gma-compare-entities", self.pre_process_event, ""),
                ("external-sgtk-launch-app", self.pre_process_event, ""),
                ("external-sgtk-initialize", self.pre_process_event, ""),
                ("sgtk-authenticated-user-changed", self.initialize_shotgun, ""),
                ("sgtk-connection-cleared-out", self.destroy_engine, ""),
            ],
            None,
        )

        self.event_queue = []
        self.event_queue_time = 0.0
        self.first_event = True
        self.semaphore = QtCore.QSemaphore(1)

        # The menu generation code makes use of the TK_RV_MODE_NAME environment
        # variable. Each menu item that is created in RV is associated with a
        # mode identified by its name. We need to make a note of our name so we
        # can add menu items for this mode later.

        os.environ["TK_RV_MODE_NAME"] = self._mode_name

        # If the shotgrid_login package already did the login, we missed the event that should
        # trigger the bootstrap.  So bootstrap now.
        if self.user is not None:
            self.initialize_shotgun()

    @property
    def server_url(self):
        return self.user.host if self.user else None

    @property
    def user(self):
        import sgtk

        return sgtk.get_authenticated_user()

    @property
    def toolkit_initialized(self):
        return self.engine is not None

    @property
    def engine(self):
        import sgtk

        return sgtk.platform.current_engine()

    def init_and_process_events(self):
        self.initialize_toolkit()

    def pre_process_event(self, event):
        self.pre_process_event_pair(event.name(), event.contents())

    def pre_process_event_pair(self, name, contents):
        if self.toolkit_initialized:
            self.process_event(name, contents)

        else:
            if self.first_event:
                msg = "Initializing ShotGrid ..."
                rve.displayFeedback2(msg, 2000.0)
                self.first_event = False

            self.event_queue += [(name, contents)]
            self.event_queue_time = rvc.theTime()

    def process_event(self, name, contents):
        print(
            "INFO: Processing event '%s' %g seconds after startup."
            % (name, rvc.theTime() - self.startup_time),
            file=sys.stderr,
        )

        if name == "external-gma-play-entity":
            self.external_gma_play_entity(name, contents)

        elif name == "external-gma-compare-entities":
            self.external_gma_compare_entities(name, contents)

        elif name == "external-sgtk-launch-app":
            self.external_launch_app(name, contents)
            rve.displayFeedback2("", 0.1)

        elif name == "external-launch-submit-tool":
            rvc.sendInternalEvent("launch-submit-tool", "")
            rve.displayFeedback2("", 0.1)

        elif name == "external-sgtk-initialize":
            rve.displayFeedback2("", 0.1)

    def process_queued_events(self):
        if self.event_queue:
            processed = []
            print(
                "INFO: Queued events waited %g seconds."
                % (rvc.theTime() - self.event_queue_time),
                file=sys.stderr,
            )
            for e in self.event_queue:
                if (e[0], e[1]) not in processed:
                    processed.append((e[0], e[1]))
                    self.process_event(e[0], e[1])

            self.event_queue = []

    def server_check(self, contents):
        gma_data = json.loads(contents)
        if "server" in gma_data:
            # print("-------------------------------- event server '%s' vs
            # '%s'\n" % (gma_data["server"], self.server_url))
            # check
            if (
                urlparse(gma_data["server"].lower()).netloc
                == urlparse(self.server_url.lower()).netloc
            ):
                return True
            else:
                print(
                    "ERROR: Server mismatch ('%s' vs '%s') "
                    "Please authenticate RV with your ShotGrid server and restart.\n"
                    % (gma_data["server"], self.server_url),
                    file=sys.stderr,
                )
                rve.displayFeedback2("", 0.1)
                return False

        return True

    def external_gma_compare_entities(self, name, contents):
        if self.server_check(contents):
            rvc.sendInternalEvent("compare_ids_from_gma", contents)
            rvc.redraw()

    def external_gma_play_entity(self, name, contents):
        if self.server_check(contents):
            internalName = "id_from_gma"

            gma_data = json.loads(contents)

            # Currently some lower-level code only supports singular "id", but
            # GMA will send (sometimes) multiple ids.  For now pull out the
            # first one and send it on.
            #
            if "ids" in gma_data and "id" not in gma_data:
                gma_data["id"] = gma_data["ids"][0]

            contents = json.dumps(gma_data)

            log.debug("callback sendEvent %s '%s'" % (internalName, contents))
            rvc.sendInternalEvent(internalName, contents)
            rvc.redraw()

    def external_launch_app(self, name, contents):
        if not self.server_check(contents):
            return

        app_data = json.loads(contents)

        if app_data["app"] == "tk-multi-importcut":
            import sgtk

            eng = sgtk.platform.current_engine()
            if eng:
                # XXX Need to check URL in data, and pass on project ID if there is one
                callback = eng.commands.get("Import Cut", dict()).get("callback")
                if callback:
                    callback()
        else:
            log.error("don't know how to launch app '%s'" % app_data["app"])

    def acquire(self):
        while self.semaphore.tryAcquire(1) is False:
            QCoreApplication.processEvents()

    # This method is used to bootstrap the Shotgun Toolkit
    # The shotgun toolkit is now enabled by default, so this method is called at launch
    # Some package will try to re-bootstrap the toolkit,
    # if the engine is already running we return without bootstraping
    # We use QSemaphore to avoid bootstrapping while there is already
    # a bootstrap in progress
    def initialize_toolkit(self):
        # bootstrap callbacks
        def completed(e):
            self.process_queued_events()
            self.semaphore.release(1)  # The bootstraping is done
            print(
                QObject().tr("INFO: Toolkit initialization took %g sec.\n")
                % (rvc.theTime() - startTime),
                file=sys.stderr,
            )
            log.debug("tk-rv bootstrapping process completed")
            rvc.sendInternalEvent("sgtk-engine-bootstrapped")

        def failure(p, e):
            self.semaphore.release(1)  # The bootstraping is done
            log.error("tk-rv bootstrapping process failed: %s" % e)

        try:
            if not self.semaphore.tryAcquire(
                1
            ):  # If the semaphore is not available, then we are already bootstrapping
                if os.environ.get(
                    "BOOTSTRAP_TK_ENGINE_SYNCHRO"
                ):  # We want to wait for the bootstrap
                    self.acquire()  # blocks until the end of other bootstrap.
                    self.semaphore.release(1)  # The bootstraping is done
                    return  # at this point we should just be able to return ?
                else:
                    return

            startTime = rvc.theTime()

            # plugin_root = os.path.join(
            #     os.path.dirname(os.path.realpath(__file__)),
            #     "..",
            #     "..",
            #     "src",
            #     "sgtk",
            #     "baked",
            #     "plugin",
            # )

            # try:
            #     from sgtk_plugin_basic_rv import manifest
            # except ImportError:
            #     sys.path.insert(0, os.path.join(plugin_root, "python"))
            #     from sgtk_plugin_basic_rv import manifest

            # core_path = manifest.get_sgtk_pythonpath(plugin_root)

            # core_path_override = os.environ.get("RV_TK_CORE")
            # if core_path_override:
            #     core_path = os.path.join(core_path_override, "python")

            # log.info("Looking for tk-core here: %s" % str(core_path))

            # # now we can kick off sgtk
            # sys.path.insert(0, core_path)
            # print(
            #     "INFO: Toolkit initialization: ready to import sgtk at %g sec."
            #     % (rvc.theTime() - startTime),
            #     file=sys.stderr,
            # )

            # import bootstrapper
            import sgtk

            # If the toolkit has been bootstrapped already (at launch)
            # and there is an engine running
            # then we can return because the goal of
            # this function is to have a running engine
            if sgtk.platform.current_engine():
                log.debug("already bootstrapped")

                self.semaphore.release(
                    1
                )  # no bootstrapping in progress, we can release
                return
            print(
                "INFO: Toolkit initialization: sgtk import complete at %g sec."
                % (rvc.theTime() - startTime),
                file=sys.stderr,
            )

            # begin logging the toolkit log tree file
            sgtk.LogManager().initialize_base_file_handler("tk-rv")

            # allow dev to override log level
            log_level = logging.WARNING
            if "RV_TK_LOG_DEBUG" in os.environ:
                log_level = logging.DEBUG

            # bind toolkit logging to our logger
            sgtk.LogManager().initialize_custom_handler(log_handler)
            # and set the level
            log_handler.setLevel(log_level)

            # Get an authenticated user object from rv's security architecture
            log.info("Will connect using %r" % self.user)

            # Now do the bootstrap!
            log.debug("Ready for bootstrap!")
            mgr = sgtk.bootstrap.ToolkitManager(self.user)
            print(
                "INFO: Toolkit initialization: ToolkitManager complete at %g sec."
                % (rvc.theTime() - startTime),
                file=sys.stderr,
            )

            # Initialize the manager using the plugin's manifest
            # manifest.initialize_manager(mgr, plugin_root)
            mgr.plugin_id = "basic.rv"

            # # If you want to take over the RV integration for development purpose,
            # # you tell Toolkit where the config is.
            # # Most likely, it's going to be the tk-config-rv folder
            # # inside the config to keep things simple.
            # config_location_override = os.environ.get("TK_CONFIG_RV_OVERRIDE")
            # if config_location_override:
            mgr.base_configuration = {"type": "app_store", "name": "tk-config-basic"}

            # tell the bootstrap API that we don't want to
            # allow for overrides from Shotgun
            entity = mgr.get_entity_from_environment()

            # Bootstrap the tk-rv engine into an empty context!

            mgr.bootstrap_engine_async(
                "tk-rv",
                entity,
                completed_callback=completed,
                failed_callback=failure,
                parent=QCoreApplication.instance(),
            )
            log.debug("Bootstrapping process started")

            # If this method is called from a command with -eval,
            # then we need to wait for the bootstrap
            # to end before we can continue, otherwise we will try
            # to use the engine before it is loaded resulting in a crash
            if os.environ.get("BOOTSTRAP_TK_ENGINE_SYNCHRO"):
                self.acquire()  # Wait for end of bootstrapping
                self.semaphore.release(1)  # bootstrapping done

        except Exception:
            print(
                "ERROR: Toolkit initialization failed.  "
                "Please authenticate RV with your ShotGrid server and restart.\n"
                + "**********************************\n",
                file=sys.stderr,
            )
            traceback.print_exc(None, sys.stderr)
            print("**********************************\n", file=sys.stderr)
            rve.displayFeedback2("", 0.1)
            # raise

    def queue_launch_import_cut_app(self, event):
        self.pre_process_event_pair(
            "external-sgtk-launch-app",
            '{"protocol_version":1,"server":"%s","app":"tk-multi-importcut"}'
            % self.server_url,
        )

    def initialize_shotgun(self, event=None):
        if event:
            event.reject()

        if self.engine:
            self.destroy_engine(event)

        if self.user:
            self.init_and_process_events()

    def launch_submit_tool(self):
        # Flag the session as "sgreview.submitInProgress" so JS submit tool
        # code can tell this is not Screening Room.
        #
        prop = "#Session.sgreview.submitInProgress"
        try:
            rvc.newProperty(prop, rvc.IntType, 1)
        except Exception:
            pass
        rvc.setIntProperty(prop, [1], True)

        rv.runtime.eval(
            """
            {
                require shotgun_mode;
                require shotgun_review_app;
                require shotgun_upload;

                if (! shotgun_mode.localModeReady())
                {
                    //  Silence the mode first, then activate it.
                    //  shotgun_mode.silent = true;
                    shotgun_mode.createLocalMode();
                }
                if (! shotgun_review_app.localModeReady())
                {
                    //  Silence the mode first, then activate it.
                    //  shotgun_review_app.silent = true;
                    shotgun_review_app.createLocalMode();
                }
                if (! shotgun_upload.localModeReady())
                {
                    //  Silence the mode first, then activate it.
                    //  shotgun_upload.silent = true;
                    shotgun_upload.createLocalMode();
                }

                shotgun_review_app.theMode().internalLaunchSubmitTool();
            }
            """,
            [],
        )

    def activate(self):
        """
        Activates the RV mode and bootstraps SGTK.
        """
        rvt.MinorMode.activate(self)

    def destroy_engine(self, event=None):
        if event:
            event.reject()

        if self.toolkit_initialized:
            import sgtk

            log.info("Shutting down engine...")
            if sgtk.platform.current_engine():
                sgtk.platform.current_engine().destroy()
            log.info("Engine is down.")

    def deactivate(self):
        """
        Deactivates the mode and tears down the currently-running
        SGTK engine.
        """
        rvt.MinorMode.deactivate(self)
        self.destroy_engine()


###############################################################################
# functions


def createMode():
    """
    Required to initialize the module. RV will call this function
    to create your mode.
    """
    return ToolkitBootstrap()


###############################################################################
# logging

log = logging.getLogger("sgtk_rv_bootstrap")
log.setLevel(logging.INFO)


# note: the RV console treats log information as html. As a consequence, all
# <html like tokens> will simply disappear when shown in the RV console. These
# <tokens> are common in toolkit, often returned by __repr__() as object identifiers.
#
# To ensure we can see everything in the RV console, html escape all log messages
# before they hit the console.
#
class EscapedHtmlFormatter(logging.Formatter):
    def __init__(self, fmt, datefmt=None):
        logging.Formatter.__init__(self, fmt, datefmt)

    def format(self, record):
        result = logging.Formatter.format(self, record)
        return html.escape(result)


log_handler = logging.StreamHandler()
log_handler.setFormatter(EscapedHtmlFormatter("%(levelname)s: %(name)s %(message)s"))
log.addHandler(log_handler)
