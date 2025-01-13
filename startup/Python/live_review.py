# Copyright (c) 2021 Autodesk.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the ShotGrid Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the ShotGrid Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by ShotGrid Software Inc.

from rv import commands, extra_commands, rvtypes, runtime, qtutils
from rv.commands import (
    NeutralMenuState,
    DisabledMenuState,
)

import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import math

import gtoContainer as gc

# Python 2 and 3 compatibility
import six

if six.PY2:

    def math_isclose(a, b, rel_tol=1e-09, abs_tol=0.0):
        return abs(a - b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)

else:

    def math_isclose(a, b, rel_tol=1e-09, abs_tol=0.0):
        return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)


from PySide2 import QtCore, QtWidgets, QtGui, QtWebEngineWidgets, QtNetwork

import otio_reader
import opentimelineio as otio
import live_review_endpoints
from live_review_diagnostic_info import DiagnosticInfo
from live_review_session_manager import SessionManager
from live_review_connectivity_manager import GlobalConnectivityManager
from live_review_logger import package_logger
from live_review_webengine_page import WebEnginePage
from live_review_analytics_interface import GlobalAnalyticsInterface
from live_review_secret_manager import SecretManager
from live_review_leave_session_dialog import LeaveSessionDialog
from live_review_latency_manager import LatencyManager

# NOTE: Bool which will help us send/receive our prefered event schema
# By default, we should be using the legacy schema
_use_legacy_schema = os.getenv("RV_LIVE_REVIEW_USE_NEW_SCHEMA") != "1"

_new_command_types = [
    "SHAREDKEY",
    "LIVE_SESSION",
    "OTIO_SESSION",
    "PLAYBACK_SETTINGS",
    "ANNOTATION",
]

_annotation_event_types = ["PAINT_START", "PAINT_POINT", "PAINT_END", "CLEAR"]

_new_event_types = [
    "GET",
    "SET",
    "NEW_PRESENTER",
    "NEW_PARTICIPANTS",
]


class LiveReviewMode(rvtypes.MinorMode, QtCore.QObject):
    def __init__(self):
        rvtypes.MinorMode.__init__(self)
        QtCore.QObject.__init__(self, QtCore.QCoreApplication.instance())

        self._connectivity_manager = GlobalConnectivityManager
        self._review_session = SessionManager()
        self._analytics = GlobalAnalyticsInterface

        self._latency_manager = LatencyManager()
        self._latency_manager.on_new_ping.connect(self.on_new_latency_manager_ping)

        self.review_session.on_new_event_signal.connect(self.process_single_event)

        self.review_session.on_new_presenter_hash_signal.connect(self.ensure_sharedkey)
        self.review_session.on_new_participants_signal.connect(
            lambda: self.ensure_sharedkey(False)
        )

        self.review_session.on_new_presenter_hash_signal.connect(self.on_new_presenter)

        self.review_session.on_request_timeout_signal.connect(self.on_request_timeout)
        self.review_session.on_request_failed_signal.connect(self.on_request_failed)
        self.review_session.on_service_broken_connection_signal.connect(
            self.on_service_broken_connection
        )
        self.connectivity_manager.on_connection_issue_threshold_signal.connect(
            self.on_connection_issue_threshold
        )

        self._user_secret = six.ensure_str(
            hashlib.sha256(
                six.ensure_binary(
                    "{}.{}".format(commands.myNetworkHost(), str(os.getpid()))
                )
            ).hexdigest()
        )

        if _use_legacy_schema is True:
            from live_review_schema_parsers import (
                LiveReviewSchemaParser as RootSchemaParser,
            )
        else:
            from live_review_schema_parsers import (
                ADSK10SchemaParser as RootSchemaParser,
            )

            package_logger.info("Currently using the new event schemas")

        self.use_legacy_schema = _use_legacy_schema

        self._post_progressive_load_callbacks = []

        # Graph event processing can only start when a RV session is loaded.
        self._can_process_graph_events = False
        # Graph events should only be sent after sending the RV session.
        self._can_send_graph_events = False
        # This is used to postponed received events until progressive loading
        # gets completed.
        self._in_progressive_loading = False

        self._temp_file_name = None
        self._sgtk_is_loaded = False

        # Review Pane UI widget
        self._reviewPaneDockWidget = None

        # Dictionary of diagnostic info requests (DiagnosticInfo)
        self._diag_info_requests = {}

        # List of gathered diagnostic info from all the participants
        self._diags = []

        self.init(
            self.menu_name.replace(" ", ""),
            self.global_bindings,
            self.local_bindings,
            self.menu,
        )

        self.override_double_arrow_menu()

        sw = qtutils.sessionWindow()

        self._reviewPaneWebView = QtWebEngineWidgets.QWebEngineView(sw)
        self._reviewPaneWebView.setVisible(False)
        self._reviewPaneWebView.setPage(WebEnginePage(parent=self._reviewPaneWebView))
        self._reviewPaneWebView.page().setBackgroundColor(QtCore.Qt.transparent)

        self._secret_manager = SecretManager()
        self._current_encryption_indicator_ui_state = None

        self.top_node = None
        self.global_start_time = otio.opentime.RationalTime()

        def err_func(origin, message):
            origin = self.get_participant_name(origin)

            if origin != "unknown":
                message = "{} ({})".format(message, origin)

            code = (
                'extra_commands.displayFeedback(qt.QObject.tr("{}"), '
                "5.0, "
                "drawStopGlyph)".format(message)
            )
            runtime.eval(code, ["glyph"])

            package_logger.error(message)

        def warn_func(origin, message):
            origin = self.get_participant_name(origin)

            if origin != "unknown":
                message = "{} ({})".format(message, origin)

            extra_commands.displayFeedback(QtCore.QObject().tr(message), 5.0)

            package_logger.warning(message)

        self._live_review_schema_parser = RootSchemaParser(
            err_func=err_func,
            warn_func=warn_func,
            secret_manager=self._secret_manager,
        )

        qtutils.javascriptExport(self._reviewPaneWebView.page())

        webengine_settings = QtWebEngineWidgets.QWebEngineSettings.globalSettings()
        webengine_settings.setAttribute(
            QtWebEngineWidgets.QWebEngineSettings.JavascriptCanAccessClipboard, True
        )

        if "RV_LIVE_REVIEW_USE_DEFAULT_REACT_DEV_URL" in os.environ:
            url = "http://127.0.0.1:4321"
        else:
            url = "file:///" + os.path.join(
                self.supportPath(sys.modules[__name__]), "index.html"
            ).replace("\\", "/")

        self._reviewPaneWebView.load(QtCore.QUrl(url))

        self.session_creator = False
        self.locally_triggered = False

    def deactivate(self):
        # When RV is closing (which deactivates the live review plugin),
        # disconnect from the current live review session if any
        if self.review_session.in_session:
            self.on_live_review_leave_session()
        rvtypes.MinorMode.deactivate(self)

    ############################################################
    # Global Properties
    ############################################################
    @property
    def connectivity_manager(self):
        return self._connectivity_manager

    @property
    def schema_parser(self):
        return self._live_review_schema_parser

    @property
    def analytics(self):
        return self._analytics

    @property
    def review_session(self):
        return self._review_session

    @property
    def menu_name(self):
        return QtCore.QObject().tr("Live Review")

    @property
    def menu(self):
        submenu = [
            (
                QtCore.QObject().tr("Toggle Panel"),
                self.toggle_review_panel,
                six.ensure_binary("control r"),
                lambda: NeutralMenuState,
            ),
            ("_", None),
            (
                QtCore.QObject().tr("Create Session"),
                self.on_create_session,
                None,
                lambda: (
                    DisabledMenuState
                    if (not self.connectivity_manager.shotgrid_connected)
                    or (not self.connectivity_manager.proxy.ready)
                    or self.review_session.in_session
                    else NeutralMenuState
                ),
            ),
            (
                QtCore.QObject().tr("Join Session"),
                self.on_join_session,
                None,
                lambda: (
                    DisabledMenuState
                    if (not self.connectivity_manager.shotgrid_connected)
                    or (not self.connectivity_manager.proxy.ready)
                    or self.review_session.in_session
                    else NeutralMenuState
                ),
            ),
        ]

        if self.review_session.in_session:
            submenu.extend(
                [
                    ("_", None),
                    (
                        QtCore.QObject().tr("Copy Invite Link"),
                        self.on_copy_link,
                        None,
                        lambda: NeutralMenuState,
                    ),
                    (
                        QtCore.QObject().tr("Copy Session ID"),
                        self.on_copy_channel,
                        None,
                        lambda: NeutralMenuState,
                    ),
                ]
            )

        submenu.extend(
            [
                ("_", None),
                (
                    QtCore.QObject().tr("Leave Session"),
                    self.on_live_review_leave_session_dialog,
                    None,
                    lambda: (
                        NeutralMenuState
                        if self.review_session.in_session
                        else DisabledMenuState
                    ),
                ),
            ]
        )

        menu = [(self.menu_name, submenu)]
        return menu

    @property
    def global_bindings(self):
        return [
            # UI and command-line access implementation
            (
                "live-review-open-review-panel",
                self.on_live_review_open_review_panel,
                "Open the live review panel",
            ),
            (
                "live-review-close-review-panel",
                self.on_live_review_close_review_panel,
                "Close the live review panel",
            ),
            (
                "live-review-copy-link",
                self.on_live_review_copy_link,
                "Copy the live review session link to clipboard",
            ),
            (
                "live-review-toggle-review-panel",
                self.on_live_review_toggle_review_panel,
                "Toggle the live review panel",
            ),
            (
                "live-review-create-session",
                self.on_live_review_create_session,
                "Create a live review session",
            ),
            (
                "live-review-join-session",
                self.on_live_review_join_session,
                "Join a live review session",
            ),
            (
                "live-review-leave-session",
                self.on_live_review_leave_session,
                "Leave a live review session",
            ),
            (
                "live-review-leave-session-dialog",
                self.on_live_review_leave_session_dialog,
                "Open Confirmation Dialog before leaving the session",
            ),
            (
                "live-review-get-session-hash",
                self.on_live_review_get_session_hash_event,
                "Get the live review session hash",
            ),
            (
                "live-review-assign-presenter",
                self.on_live_review_assign_presenter,
                "Assign the participant the presenter role",
            ),
            (
                "live-review-is-review-panel-open",
                self.on_live_review_is_review_panel_open,
                "Returns 'True' if the live review panel is open, 'False' otherwise.",
            ),
            (
                "live-review-translate",
                self.on_live_review_translate,
                "Returns the translation of the string passed as parameter.",
            ),
            (
                "live-review-feedback-panel-open",
                self.on_live_review_feedback_panel_open,
                "Trigger amplitude event when the panel is opened",
            ),
            (
                "live-review-feedback-form-submitted",
                self.on_live_review_feedback_form_submitted,
                "Submit the form data to the service",
            ),
            # Diagnostic information
            (
                "live-review-request-diagnostic-info",
                self.on_live_review_request_diagnostic_info,
                "Initate a request to gather diagnostic info from all the participants",
            ),
            (
                "live-review-get-diagnostic-info",
                self.on_live_review_get_diagnostic_info,
                "Returns diagnostic info",
            ),
            # RV Sync transport interface implementation
            # For reference : live_review_sync.mu
            (
                "sync-transport-send-event",
                self.on_sync_transport_send_event_event,
                "Send an event to the live review service",
            ),
            (
                "sync-transport-push-session",
                self.on_sync_transport_push_session_event,
                "Push a RV session to all other clients",
            ),
            (
                "after-progressive-loading",
                self.on_after_progressive_loading_event,
                "Handle post-progressive loading to unblock actions.",
            ),
            (
                "live-review-ui-component-loaded",
                self.live_review_ui_component_loaded_event,
                "Update so that UI can display the live review session details",
            ),
            (
                "live-review-open-beta-forum",
                self.open_beta_forums_event,
                "Open the beta forums in a default web browser",
            ),
            (
                "live-review-retry-connection",
                self.on_live_review_retry_connection,
                "Retry network connection and update UI if connected to internet",
            ),
            (
                "live-review-display-feedback",
                self.on_live_review_display_feedback,
                "Display the HUD notification over the player",
            ),
            (
                "live-review-blocked-event",
                self.on_live_review_blocked_event,
                "An event was blocked for the non-presenter",
            ),
            # External events handling (e.g. Landing page)
            (
                "external-launch-live-review-session",
                self.on_external_launch_live_review_session_event,
                "Join a live review session from the landing page",
            ),
            # sgtk events
            (
                "sgtk-authenticated-user-changed",
                self.on_sgtk_authenticated_user_changed_event,
                "The sgtk authenticated user has changed",
            ),
            (
                "sgtk-connection-cleared-out",
                self.on_sgtk_connection_cleared_out_event,
                "The sgtk connection is cleared out",
            ),
        ]

    @property
    def local_bindings(self):
        return [
            (
                "key-down--control--r",
                self.toggle_review_panel,
                "Toggle Live Review Panel",
            ),
        ]

    # NOTE: The old/new handlers are grouped together,
    # the end goal is to only use the new schema handlers (<COMMAND> <EVENT>)
    @property
    def special_read_handler(self):
        """Live Review Event Handlers"""
        return {
            "mq-request-session-push": self.mq_request_session_handler,
            "OTIO_SESSION GET": self.mq_request_session_handler,
            # TODO: Merge the two handlers into one
            "mq-session": self.mq_session_handler,
            "OTIO_SESSION SET": self.mq_web_session_handler,
            "mq-new-presenter": self.mq_new_presenter_handler,
            "LIVE_SESSION NEW_PRESENTER": self.mq_new_presenter_handler,
            "mq-new-participants": self.mq_new_participants_handler,
            "LIVE_SESSION NEW_PARTICIPANTS": self.mq_new_participants_handler,
            "mq-diag-info-request": self.mq_diag_info_request_handler,
            "mq-diag-info-event": self.mq_diag_info_event_handler,
            "mq-diag-info-response": self.mq_diag_info_response_handler,
            "mq-request-sync-playback": self.mq_request_sync_playback_handler,
            "PLAYBACK_SETTINGS GET": self.mq_request_sync_playback_handler,
            # TODO: Merge the two handlers into one
            "mq-sync-playback": self.mq_sync_playback_handler,
            "PLAYBACK_SETTINGS SET": self.mq_web_sync_playback_handler,
            "mq-sharedkey-request": self.mq_sharedkey_request_handler,
            "SHAREDKEY GET": self.mq_sharedkey_request_handler,
            "mq-sharedkey-response": self.mq_sharedkey_response_handler,
            "SHAREDKEY SET": self.mq_sharedkey_response_handler,
            "mq-latency": self.mq_latency_handler,
        }

    @property
    def playback_settings_handler(self):
        """Playback Settings Event Handlers"""
        return {
            "looping": self.pb_settings_looping_handler,
            "playing": self.pb_settings_playing_handler,
            "muted": self.pb_settings_muted_handler,
            "playback_range": self.pb_settings_range_handler,
            "current_time": self.pb_settings_time_handler,
            "scrubbing": self.pb_settings_scrubbing_handler,
            "output_bounds": self.pb_settings_bounds_handler,
        }

    @property
    def annotations_handler(self):
        """Annotation Event Handlers"""
        return {
            "PAINT_START": self.paint_start_handler,
            "PAINT_POINT": self.paint_point_handler,
            "PAINT_END": self.paint_end_handler,
            "CLEAR": self.clear_handler,
        }

    ############################################################
    # Menu items implementation
    ############################################################

    def on_create_session(self, event):
        commands.sendInternalEvent("live-review-create-session")

    def on_join_session(self, event, session_id=""):
        sw = qtutils.sessionWindow()
        session_id, ok = QtWidgets.QInputDialog.getText(
            sw,
            QtCore.QObject().tr("Session ID"),
            QtCore.QObject().tr("Session ID"),
            text=session_id,
        )
        if ok:
            # extra_commands.displayFeedback(
            #     QtCore.QObject().tr("Joining Session..."), 3.0
            # )
            # self.join_session(session_id)
            package_logger.info(f"LIVE REVIEW: on_join_session {session_id}")
            commands.sendInternalEvent("live-review-join-session", str(session_id))
        else:
            package_logger.info("Connect Dialog Cancelled")

        # self.analytics.log_amplitude_event(
        #     "Join Session", {"source": "Internal", "from": "Menu"}
        # )

    def on_copy_link(self, event=None):
        session_hash = self._review_session.session_id
        if not session_hash:
            package_logger.warning(
                "Unable to copy Live Review session link without a session Id."
            )
            return False

        url = "%s/live/%s" % (self.review_session.proxy.host, session_hash)
        QtWidgets.QApplication.clipboard().setText(url)
        self.analytics.log_amplitude_event("Link Copied")
        package_logger.info("Copied Live Review sesion link to clipboard : %s" % url)
        return True

    def on_copy_channel(self, event=None):
        if not self.review_session.in_session:
            package_logger.warning("Unable to copy session ID without a set channel.")
            return

        QtWidgets.QApplication.clipboard().setText(self.review_session.session_id)
        self.analytics.log_amplitude_event("Session ID Copied from menu")
        package_logger.info("Session ID copied to clipboard.")

    ############################################################
    # Global bindings implementation
    ############################################################

    # UI and command-line access implementation

    def on_live_review_open_review_panel(self, event=None):
        self.open_review_panel()

    def on_live_review_close_review_panel(self, event=None):
        self.close_review_panel()

    def on_live_review_copy_link(self, event=None):
        result = self.on_copy_link()
        if event:
            event.setReturnContent("success" if result else "")

    def on_live_review_toggle_review_panel(self, event=None):
        self.toggle_review_panel()

    def on_live_review_create_session(self, event=None):
        event.reject()
        package_logger.info("LIVE REVIEW: Create Session")

        extra_commands.displayFeedback(QtCore.QObject().tr("Creating Session..."), 3.0)
        self.create_session()

    def on_live_review_join_session(self, event=None):
        event.reject()
        package_logger.info(f"LIVE REVIEW: Join Session {event.contents()}")

        extra_commands.displayFeedback(QtCore.QObject().tr("Joining Session..."), 3.0)
        self.analytics.log_amplitude_event(
            "Join Session", {"source": "Internal", "from": "Panel"}
        )
        session_id = event.contents()
        self.join_session(session_id)

    def on_live_review_leave_session(self, event=None):
        event.reject()
        package_logger.info("LIVE REVIEW: Leave Session")

        if self.review_session.in_session:
            extra_commands.displayFeedback(
                QtCore.QObject().tr("Leaving Session..."), 3.0
            )
            self.quit_review()
            self.analytics.log_amplitude_event("Leave Session")

    def on_live_review_leave_session_dialog(self, event=None):
        if self.review_session.in_session:
            if (
                len(self.review_session.participants) > 2
                and self.review_session.is_presenter
            ):
                leave_session_dialog = LeaveSessionDialog()
                leave_session_dialog.exec_()
            else:
                # self.on_live_review_leave_session()
                commands.sendInternalEvent("live-review-leave-session")

    def on_live_review_get_session_hash_event(self, event=None):
        session_id = self._review_session.session_id
        if event:
            event.setReturnContent(session_id)
        return session_id

    def on_live_review_is_review_panel_open(self, event=None):
        is_open = self._reviewPaneDockWidget and self._reviewPaneDockWidget.isVisible()
        ret_content = "True" if is_open else "False"
        if event:
            event.setReturnContent(ret_content)
        return ret_content

    def on_live_review_translate(self, event=None):
        ret_content = QtCore.QObject().tr(event.contents())
        if event:
            event.setReturnContent(ret_content)
        return ret_content

    def on_live_review_feedback_panel_open(self, event=None):
        self.analytics.log_amplitude_event("Feedback Form Opened")
        return

    def on_live_review_feedback_form_submitted(self, event=None):
        def feedback_submitted_cb(url, error, data):
            if error == QtNetwork.QNetworkReply.NoError:
                extra_commands.displayFeedback(
                    QtCore.QObject().tr("Thanks for your feedback."), 3.0
                )
                self.analytics.log_amplitude_event("Feedback Form Submitted", params)

        params = json.loads(event.contents())
        self._review_session.submit_feedback(params, callback=feedback_submitted_cb)

        return

    def open_beta_forums_event(self, event=None):
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl(live_review_endpoints.feedback_forum_endpoint())
        )

    def live_review_ui_component_loaded_event(self, event=None):
        commands.sendInternalEvent(
            "live-review-shotgrid-connectivity-changed",
            str(self.connectivity_manager.shotgrid_connected).lower(),
        )
        commands.sendInternalEvent(
            "live-review-connectivity-changed",
            str(self.connectivity_manager.network_connected).lower(),
        )
        commands.sendInternalEvent(
            "live-review-shotgrid-url-changed", self.connectivity_manager.shotgrid_url
        )

        commands.sendInternalEvent(
            "live-review-session-id-changed", self.review_session.session_id
        )
        commands.sendInternalEvent(
            "live-review-session-hash-changed", self.review_session.session_hash
        )
        commands.sendInternalEvent(
            "live-review-participant-hash-changed",
            self.review_session.participant_hash,
        )
        commands.sendInternalEvent(
            "live-review-participants-changed",
            json.dumps(self.review_session.participants, sort_keys=True),
        )
        commands.sendInternalEvent(
            "live-review-presenter-hash-changed", self.review_session.presenter_hash
        )

        if self.on_live_review_is_review_panel_open() == "True":
            self.connectivity_manager.check_connection()

    def on_live_review_assign_presenter(self, event=None):
        event and event.reject()
        package_logger.error("LIVE REVIEW: Assigning presenter")

        participant_hash = event.contents()

        def assign_presenter_cb(url, error, data):
            if error == QtNetwork.QNetworkReply.NoError:
                if not self.review_session.is_presenter:
                    # Notify the presenter that he's the presenter
                    if self.use_legacy_schema:
                        self.send_event(
                            "mq-new-presenter", participant_hash, participant_hash
                        )
                    else:
                        self.send_event(
                            "NEW_PRESENTER",
                            participant_hash,
                            participant_hash,
                            "LIVE_SESSION",
                        )

        # Ensure that we stores the valid information.
        # In other words, this needs to be done before assigning the presenter.
        action_by_current_presenter = self.review_session.is_presenter
        assigning_myself = self.review_session.participant_hash == participant_hash

        self.review_session.assign_presenter(
            participant_hash, callback=assign_presenter_cb
        )

        self.analytics.log_amplitude_event(
            "Assign Presenter",
            {
                "action_by_current_presenter": action_by_current_presenter,
                "assigning_myself": assigning_myself,
            },
        )

    def on_live_review_request_diagnostic_info(self, event=None):
        # Clear any existing list of diagnostic info
        self._diags = []

        # Nothing to do if there is no review session in progress
        if not self.review_session.in_session:
            return

        # Are we the presenter ?
        if self.review_session.is_presenter:
            # We are the presenter: we can broadcast
            # Broadcast a diagnostic info request to all the participants
            self.send_event(
                "mq-diag-info-request", DiagnosticInfo.DIAGNOSTIC_INFO_VERSION
            )

            # Broadcast a specific number of diagnostic info events to measure
            # latency and to detect out of order events or missed events
            for e in range(DiagnosticInfo.DIAGNOSTIC_INFO_EVENTS_COUNT):
                event_info = {
                    "index": e,
                    "time_sent": time.time(),
                }
                self.send_event("mq-diag-info-event", json.dumps(event_info))
        else:
            # We are not the presenter: we need to use direct messages
            for p in self.review_session.participants:
                participant_hash = p["ParticipantHash"]
                if participant_hash == self.review_session.participant_hash:
                    continue

                # Send a diagnostic info request to the participant
                self.send_event(
                    "mq-diag-info-request",
                    DiagnosticInfo.DIAGNOSTIC_INFO_VERSION,
                    participant_hash,
                )

                # Send a specific number of diagnostic info events to measure
                # latency and to detect out of order events or missed events
                for e in range(DiagnosticInfo.DIAGNOSTIC_INFO_EVENTS_COUNT):
                    event_info = {
                        "index": e,
                        "time_sent": time.time(),
                    }
                    self.send_event(
                        "mq-diag-info-event", json.dumps(event_info), participant_hash
                    )

        if event:
            event.setReturnContent("True")

    def on_live_review_get_diagnostic_info(self, event=None):
        if event:
            event.setReturnContent(json.dumps(self._diags))

        return self._diags

    def on_live_review_retry_connection(self, event=None):
        self.connectivity_manager.check_connection()

    def on_live_review_display_feedback(self, event=None):
        extra_commands.displayFeedback(QtCore.QObject().tr(event.contents()), 3.0)

    def on_live_review_blocked_event(self, event=None):
        code = """
            extra_commands.displayFeedback(qt.QObject.tr("You are not the presenter")
            , 3.0, drawStopGlyph)
        """
        runtime.eval(code, ["glyph"])

    # RV Sync transport interface implementation
    def on_sync_transport_send_event_event(self, event):
        contents = event.contents()
        (eventName, payload) = contents.split(";;", 1)
        self.send_event(eventName, payload)
        event.setReturnContent("True")

    def on_sync_transport_push_session_event(self, event):
        event.setReturnContent("True")
        self.push_session_to_queue(self.get_current_session())

    def on_after_progressive_loading_event(self, event):
        event.reject()
        self._in_progressive_loading = False

        for callback in self._post_progressive_load_callbacks:
            callback()

        self._post_progressive_load_callbacks = []

    def on_external_mqsync_connect_event(self, event):
        session_id = event.contents()
        self.join_session(session_id)

    # External events handling (e.g. Landing page)

    def on_external_launch_live_review_session_event(self, event):
        params = json.loads(event.contents())
        self.on_external_launch_live_review_session(params)

    def on_external_launch_live_review_session(self, params):
        # First make sure that sgtk has finished loading
        # Retry later if sgtk hasn't finished loading yet
        # Note : this external event comes from an rvlink such as the Live
        # Review landing page for example. The Live Review plugin requires the
        # functionality of sgtk. However sgtk is loaded asynchronously so
        # it is possible that it hasn't finished loading when the Live Review
        # plugin is being asked to launch a live review session.
        if not self._sgtk_is_loaded:
            try:
                import sgtk

                self._sgtk_is_loaded = sgtk.get_authenticated_user() is not None
            except:
                pass
            if not self._sgtk_is_loaded:
                timer = QtCore.QTimer()
                timer.setInterval(0)
                timer.timeout.connect(
                    lambda: self.on_external_launch_live_review_session(params)
                )
                timer.timeout.connect(lambda: timer.deleteLater())
                timer.setSingleShot(True)
                timer.start()
                return

        session_id = params["sessionId"]
        if session_id:
            self.analytics.log_amplitude_event("Join Session", {"source": "External"})
            self.open_review_panel()
            self.join_session(session_id)

    # sgtk events handling

    def on_sgtk_authenticated_user_changed_event(self, event):
        package_logger.debug(
            "The sgtk authenticated user has changed: renewing credentials..."
        )
        event.reject()  # Do not consume event to allow others to be notified

        commands.sendInternalEvent(
            "live-review-shotgrid-url-changed", self.connectivity_manager.shotgrid_url
        )

        self.connectivity_manager.proxy.renew_credentials(
            refresh_token_if_possible=False
        )

        self.quit_review()

    def on_sgtk_connection_cleared_out_event(self, event):
        package_logger.debug("Sgtk connection is cleared out.")
        event.reject()
        self.connectivity_manager.shotgrid_connected = False
        commands.defineModeMenu(self.menu_name.replace(" ", ""), self.menu, True)
        self.quit_review()

    ############################################################
    # Local bindings implementation
    ############################################################

    # Empty

    ############################################################
    # Special read handler implementation
    ############################################################

    def bad_origin_handler(self, origin, payload):
        package_logger.info(
            "Session from {} but not the presenter, fetching the current presenter...".format(
                origin
            )
        )

        def fetch_participants_cb(url, error, data):
            if (
                error == QtNetwork.QNetworkReply.NoError
                and self.review_session.presenter_hash == origin
            ):
                if self.review_session.presenter_hash == origin:
                    package_logger.info(
                        "{} is the current the presenter. Loading the session".format(
                            origin
                        )
                    )
                    self.mq_session_handler(origin, payload)
                else:
                    package_logger.info(
                        "{} is not the current presenter. Dropping the session.".format(
                            origin
                        )
                    )

        return self.review_session.fetch_participants(callback=fetch_participants_cb)

    # Received by a Participant
    def mq_web_session_handler(self, origin, payload):
        # Only presenter can send an RV session, update the presenter accordingly
        if not self.review_session.presenter_hash == origin:
            return self.bad_origin_handler(origin, payload)

        commands.sendInternalEvent(
            "internal-sync-presenter-changed", self.serialized_participant_info(origin)
        )

        # TODO: Add a way to check if the session in payload is the same as the current session.
        # Idea: First time connecting to a Web Review Session, save the OTIO received as a sha and
        # put that in a var used to compared against other OTIO if the session ever changes

        if "tracks" not in payload:
            package_logger.info("Clearing; preparing to receive a Web Review Session")

            commands.clearSession()

            self._in_progressive_loading = False
            self._can_process_graph_events = False
        else:
            package_logger.info("Loading session from {}".format(origin))

            # Start accepting graph events if we were not already.
            # If required, all events will be postponed until the progressive
            # loading is completed.
            self._in_progressive_loading = True
            self._can_process_graph_events = True

            # Parse the OTIO JSON Payload and save it as the current session (Node Graph)
            # Also save some useful vars on the return
            self.top_node, self.global_start_time = otio_reader.read_otio_json(
                payload, self.review_session.proxy.host
            )

            package_logger.info("Loading session from Web Review")

            self.send_event("GET", "", origin, "PLAYBACK_SETTINGS")

    # Received by a Participant
    def mq_session_handler(self, origin, payload):
        # Only presenter can send an RV session, update the presenter accordingly
        if not self.review_session.presenter_hash == origin:
            return self.bad_origin_handler(origin, payload)

        package_logger.info("Loading session from {}".format(origin))

        commands.sendInternalEvent(
            "internal-sync-presenter-changed", self.serialized_participant_info(origin)
        )

        (fps, frame, play_mode, session_info) = payload.split(";;", 3)

        # Whether the name can be used to open the file a second time,
        # while the named temporary file is still open, varies across
        # platforms (it can be so used on Unix; it cannot on Windows NT
        # or later)
        with tempfile.NamedTemporaryFile(
            prefix="LiveReview-", suffix=".rv", mode="wb", delete=False
        ) as temp_file:
            self._temp_file_name = temp_file.name.replace("\\", "/")
            session_info = json.loads(session_info)
            temp_file.write(bytearray(session_info))
            temp_file.flush()

        # Avoid reloading the presenter's session if it is the same as the
        # current session.
        if self.compare_with_current_session(self._temp_file_name):
            # The presenter's session is the same as the current session
            # Note that we still set the current frame as it is not taken
            # account when comparing both sessions.
            package_logger.info(
                "New RV session is the same as the current one - no need to reload session"
            )
            self._can_process_graph_events = True
            commands.setFrame(int(frame))
            return

        package_logger.info(
            "New RV session is different from the current one - loading new session"
        )

        # Start accepting graph events if we were not already.
        # If required, all events will be postponed until the progressive
        # loading is completed.
        self._in_progressive_loading = True
        self._can_process_graph_events = True

        try:
            fps = float(fps)
        except ValueError:
            pass
        else:

            def set_output_fps():
                try:
                    commands.setFloatProperty(
                        six.ensure_str("#RVRetime.output.fps"), [fps]
                    )
                except:
                    pass

            self._post_progressive_load_callbacks.append(set_output_fps)

        try:
            frame = int(frame)
        except ValueError:
            pass
        else:
            self._post_progressive_load_callbacks.append(
                lambda: commands.setFrame(frame)
            )

        self._post_progressive_load_callbacks.append(
            lambda: commands.setPlayMode(int(play_mode))
        )

        self._post_progressive_load_callbacks.append(lambda: self.sync_playback())

        commands.addSources([self._temp_file_name], "explicit", False, False)

    # Received by a Presenter
    def mq_request_session_handler(self, origin, payload):
        self.push_session_to_queue(self.get_current_session(), origin)

    def mq_diag_info_request_handler(self, origin, payload):
        self._diag_info_requests[origin] = DiagnosticInfo(
            payload,
            origin,
            self.review_session.participant_hash,
            self,
        )

    def mq_diag_info_event_handler(self, origin, payload):
        if origin in self._diag_info_requests:
            self._diag_info_requests[origin].on_event(payload)

    def mq_diag_info_response_handler(self, origin, payload):
        diagnostic_info = json.loads(payload)

        # Find the participant's name
        participant_name = self.get_participant_name(
            diagnostic_info["participant_hash"]
        )

        if diagnostic_info["compatible_version"] == "yes":
            package_logger.info(
                "Received diagnostic info : latency = %s ms, from participant = %s (%s)"
                % (
                    diagnostic_info["latency_median_in_ms"],
                    participant_name,
                    diagnostic_info["participant_hash"],
                )
            )
        else:
            package_logger.info(
                "Received diagnostic info : incompatible version, from participant = %s (%s)"
                % (participant_name, diagnostic_info["participant_hash"])
            )

        self._diags.append(diagnostic_info)

    # Received by a Presenter
    def mq_request_sync_playback_handler(self, origin, payload):
        response = "%s;;%s;;%s" % (
            str(commands.frame()),
            str(commands.isPlaying()),
            str(commands.playMode()),
        )
        self.send_event("mq-sync-playback", response, participant_hash=origin)

    # Received by a Participant
    def mq_web_sync_playback_handler(self, origin, payload):
        playback_settings_obj = json.loads(payload)

        for key, payload in playback_settings_obj.items():
            if key not in self.playback_settings_handler:
                package_logger.info("Property '{}' has no handler".format(key))
                continue
            self.playback_settings_handler[key](origin, payload)

    # Received by a Participant
    def mq_sync_playback_handler(self, origin, payload):
        (frame, is_playing, play_mode) = payload.split(";;", 2)

        commands.setPlayMode(int(play_mode))
        commands.setFrame(int(frame))

        if is_playing == "True":
            commands.play()
        else:
            commands.stop()

    def mq_new_presenter_handler(self, origin, payload):
        self.review_session.presenter_hash = payload
        self.review_session.fetch_participants()

    def mq_new_participants_handler(self, origin, payload):
        if self.review_session.is_presenter:
            if self.use_legacy_schema:
                self.send_event("mq-new-participants")
            else:
                self.send_event("NEW_PARTICIPANTS", command="LIVE_SESSION")
        self.review_session.fetch_participants()

    # Received by a Presenter
    def mq_sharedkey_request_handler(self, origin, payload):
        if self.review_session.is_presenter:
            package_logger.info(
                "Received shared key request from participant %s" % (origin)
            )
            if self.use_legacy_schema:
                pem = six.ensure_str(base64.b64decode(payload))
            else:
                pem = six.ensure_str(payload)

            shared_key_payload = six.ensure_str(
                base64.b64encode(self._secret_manager.encrypted_shared_key(pem))
            )

            if self.use_legacy_schema:
                self.send_event(
                    "mq-sharedkey-response",
                    shared_key_payload,
                    origin,
                )
            else:
                self.send_event("SET", shared_key_payload, origin, "SHAREDKEY")
        else:
            package_logger.info(
                "Received shared key request from participant %s"
                "but wont process it as curently not the presenter." % (origin)
            )

    # Received by a Participant
    def mq_sharedkey_response_handler(self, origin, payload):
        # Check that the shared_key is received by the expected presenter for safety
        if self.review_session.presenter_hash == origin:
            package_logger.info(
                "Received shared key response from presenter %s" % origin
            )
            encrypted_shared_key = six.ensure_str(base64.b64decode(payload))

            # If the received shared key is the same as the one currently set up,
            # we don't need to request for the session to the presenter (and to
            # save the received shared key either).
            if not self._secret_manager.is_new_shared_key(encrypted_shared_key):
                package_logger.debug(
                    "Received shared key is the same as the one already in use."
                )
                return

            self._secret_manager.save_shared_key(encrypted_shared_key)

            if self.use_legacy_schema:
                self.send_event(
                    "mq-request-session-push",
                    "",
                    self.review_session.presenter_hash,
                )
            else:
                self.send_event(
                    "GET", "", self.review_session.presenter_hash, "OTIO_SESSION"
                )

            # Update the indicator since encrypted requests likely
            # become possible after setting up the shared key.
            self.update_encryption_indicator_ui()
        else:
            package_logger.warning(
                (
                    "Shared key not received from expected presenter. "
                    "Received from: %s. "
                    "Expected from: %s"
                )
                % (origin, self.review_session.presenter_hash)
            )

    def mq_latency_handler(self, origin, payload):
        payload = LatencyManager.tag_end(payload)
        self._latency_manager.process_ping(payload)

    ############################################################
    # Playback settings read handler implementation
    ############################################################

    def pb_settings_looping_handler(self, origin, payload):
        if payload is True:
            commands.setPlayMode(0)  # PlayLoop
        else:
            commands.setPlayMode(1)  # PlayOnce

    def pb_settings_playing_handler(self, origin, payload):
        if payload is True:
            commands.play()
        else:
            commands.stop()

    def pb_settings_muted_handler(self, origin, payload):
        # Handled by the client, for now...
        pass

    def pb_settings_range_handler(self, origin, payload):
        if "zoomed" in payload:
            if payload["zoomed"] is True:
                # TODO: Does it like Web Review, but maybe seperate the 2 checks and set to outPoint if farther than it
                if (
                    commands.frame() < commands.inPoint()
                    or commands.frame() > commands.outPoint()
                ):
                    commands.setFrame(commands.inPoint())

        if "enabled" in payload:
            # Nothing to do with this, or maybe?????
            pass

        if "range" in payload:
            start_time = otio.opentime.RationalTime(
                payload["range"]["start_time"]["value"],
                payload["range"]["start_time"]["rate"],
            )
            duration = otio.opentime.RationalTime(
                payload["range"]["duration"]["value"],
                payload["range"]["duration"]["rate"],
            )

            # We need to rescale everything to the first clip's FPS
            framerate = commands.fps()

            # RV uses relative time, so we're shifting everybody to index 1
            start_time -= self.global_start_time  # Shift back
            start_time = start_time.rescaled_to(framerate)  # Rescale
            start_time += otio.opentime.RationalTime(
                1, framerate
            )  # Shift forward to start at index 1

            duration = duration.rescaled_to(framerate)  # Rescale
            # duration += otio.opentime.RationalTime(1, framerate) # Shift forward

            playback_range = otio.opentime.TimeRange(start_time, duration)

            commands.setInPoint(math.floor(playback_range.start_time.value + 0.5))
            commands.setOutPoint(
                math.floor(playback_range.end_time_inclusive().value + 0.5)
            )

    def pb_settings_time_handler(self, origin, payload):
        current_time = otio.opentime.RationalTime(payload["value"], payload["rate"])

        # TODO update other settings if we change clip (eg. the aspect ratio may change from clip to clip)
        # We need to rescale everything to the first clip's FPS
        framerate = commands.fps()

        # RV uses relative time, so we're shifting everybody to index 1
        current_time -= self.global_start_time  # Shift back
        current_time = current_time.rescaled_to(framerate)  # Rescale
        current_time += otio.opentime.RationalTime(1, framerate)  # Shift forward

        # Make sure to round to the closest int (unlike .to_frames()...)
        commands.setFrame(math.floor(current_time.value + 0.5))

    def pb_settings_scrubbing_handler(self, origin, payload):
        # Naht needed, or is it...?
        pass

    def pb_settings_bounds_handler(self, origin, payload):
        if not self._can_process_graph_events:
            return

        sources = commands.sourcesAtFrame(commands.frame())

        # Get the clip width/height because the annotation source
        # has width and height equal to 1 (which messes with the scaling)
        width = commands.sourceMediaInfo(sources[-1])["width"]
        height = commands.sourceMediaInfo(sources[-1])["height"]
        aspect_ratio = 1.0 if height == 0 else width / height

        # TODO: Use parser to get the globals, which requires 'available_image_bounds' in the OTIO
        # NOTE: 'available_image_bounds' appears in the Clip.2 schema
        global_scale = otio.schema.V2d(
            1.0 / 9, 1.0 / 9
        )  # [1.0 / scale.y, 1.0 / scale.y]
        global_translate = otio.schema.V2d(0, 0) * global_scale

        bounding_box = otio.schema.Box2d(
            otio.schema.V2d(payload[0] - payload[2] / 2, payload[1] - payload[3] / 2),
            otio.schema.V2d(payload[0] + payload[2] / 2, payload[1] + payload[3] / 2),
        )

        translate = bounding_box.center() * global_scale - global_translate
        scale = (bounding_box.max - bounding_box.min) * global_scale

        # TODO: Add translate scaling when the media is smaller or larger
        # The translate should scale accordingly

        for source in sources:
            transform_node = extra_commands.associatedNode("RVTransform2D", source)
            commands.setFloatProperty(
                "{}.transform.scale".format(transform_node),
                [
                    aspect_ratio / (scale.x if scale.x != 0.0 else aspect_ratio),
                    1 / (scale.y if scale.y != 0.0 else 1.0),
                ],
            )
            commands.setFloatProperty(
                "{}.transform.translate".format(transform_node),
                [-translate.x, -translate.y],
            )

    ############################################################
    # Annotation handler implementation
    ############################################################

    def paint_start_handler(self, origin, payload):
        paint = otio.adapters.read_from_string(payload)["paint"]

        frame = commands.frame()

        rv_node = extra_commands.nodesInGroupOfType("tracks", "RVPaint")[0]
        paint_node = "{}.paint".format(rv_node)
        stroke = commands.getIntProperty("{}.nextId".format(paint_node))[0]
        pen_node = "{}.pen:{}:{}:berniet".format(rv_node, stroke, frame)

        # BUG: Opacity is a bit wonky
        # There are too many points overlapping during a stroke, which means that the opacity
        # from each points add up to 1.0 after like 2-3 overlapping points.

        # Add and set props on the .pen node
        if not commands.propertyExists("{}.brush".format(pen_node)):
            commands.newProperty("{}.brush".format(pen_node), commands.StringType, 1)

        commands.setStringProperty(
            "{}.brush".format(pen_node), [paint.brush.lower()], True
        )

        if not commands.propertyExists("{}.color".format(pen_node)):
            commands.newProperty("{}.color".format(pen_node), commands.FloatType, 4)

        commands.setFloatProperty(
            "{}.color".format(pen_node), [float(x) for x in paint.rgba], True
        )

        if not commands.propertyExists("{}.debug".format(pen_node)):
            commands.newProperty("{}.debug".format(pen_node), commands.IntType, 1)

        commands.setIntProperty("{}.debug".format(pen_node), [False], True)

        if not commands.propertyExists("{}.join".format(pen_node)):
            commands.newProperty("{}.join".format(pen_node), commands.IntType, 1)

        commands.setIntProperty("{}.join".format(pen_node), [3], True)

        if not commands.propertyExists("{}.cap".format(pen_node)):
            commands.newProperty("{}.cap".format(pen_node), commands.IntType, 1)

        commands.setIntProperty("{}.cap".format(pen_node), [2], True)

        if not commands.propertyExists("{}.splat".format(pen_node)):
            commands.newProperty("{}.splat".format(pen_node), commands.IntType, 1)

        commands.setIntProperty("{}.splat".format(pen_node), [1], True)

    def paint_point_handler(self, origin, payload):
        point = otio.adapters.read_from_string(payload)["point"]

        frame = commands.frame()

        rv_node = extra_commands.nodesInGroupOfType("tracks", "RVPaint")[0]
        paint_node = "{}.paint".format(rv_node)
        stroke = commands.getIntProperty("{}.nextId".format(paint_node))[0]
        pen_node = "{}.pen:{}:{}:berniet".format(rv_node, stroke, frame)
        frame_node = "{}.frame:{}".format(rv_node, frame)

        global_scale = otio.schema.V2d(1.0 / 9, 1.0 / 9)  # 0.1111111...
        global_width = 2 / 15  # 0.133333...

        if not commands.propertyExists("{}.order".format(frame_node)):
            commands.newProperty("{}.order".format(frame_node), commands.StringType, 1)

        commands.insertStringProperty(
            "{}.order".format(frame_node), ["pen:{}:{}:berniet".format(stroke, frame)]
        )

        if not commands.propertyExists("{}.points".format(pen_node)):
            commands.newProperty("{}.points".format(pen_node), commands.FloatType, 2)
        commands.insertFloatProperty(
            "{}.points".format(pen_node),
            [point.x * global_scale.x, point.y * global_scale.y],
        )

        if not commands.propertyExists("{}.width".format(pen_node)):
            commands.newProperty("{}.width".format(pen_node), commands.FloatType, 1)

        commands.insertFloatProperty(
            "{}.width".format(pen_node), [point.width * global_width]
        )

    def paint_end_handler(self, origin, payload):
        rv_node = extra_commands.nodesInGroupOfType("tracks", "RVPaint")[0]

        paint_node = "{}.paint".format(rv_node)
        stroke = commands.getIntProperty("{}.nextId".format(paint_node))[0]

        # Set props on the .paint node
        commands.setIntProperty("{}.nextId".format(paint_node), [stroke + 1], True)
        commands.setIntProperty("{}.show".format(paint_node), [True], True)

    def clear_handler(self, origin, payload):
        pass
        # TODO: Implement annotation clear functionnality
        # rv_node = extra_commands.nodesInGroupOfType('tracks', "RVPaint")[0]

        # clear_obj = otio.adapters.read_from_string(payload)

        # if clear_obj["clearAll"] is True:
        #     # For each tracks_paint:frame:<>
        #     # 1. Get .order property contents (getStringProperty)
        #     # 2. Create the .redo property
        #     # 3. Insert the contents of .order inside
        #     # 4. Update the .order property (empty it)
        #     pass

        # if "target" in clear_obj:
        #     start_time = otio.opentime.RationalTime(
        #         clear_obj['target']['range']['start_time']['value'],
        #         clear_obj['target']['range']['start_time']['rate']
        #     )
        #     duration = otio.opentime.RationalTime(
        #         clear_obj['target']['range']['duration']['value'],
        #         clear_obj['target']['range']['duration']['rate']
        #     )

        #     # We need to rescale everything to the first clip's FPS
        #     framerate = commands.fps()
        #     # RV uses relative time, so we're shifting everybody to index 1
        #     start_time -= self.global_start_time # Shift back
        #     start_time = start_time.rescaled_to(framerate) # Rescale
        #     start_time += otio.opentime.RationalTime(1, framerate) # Shift forward to start at index 1

        #     duration = duration.rescaled_to(framerate) # Rescale
        #     # duration += otio.opentime.RationalTime(1, framerate) # Shift forward

        #     clear_range = otio.opentime.TimeRange(start_time, duration)

        #     frame = clear_range.end_time_exclusive().to_frames()
        #     frame_node = '{}.frame:{}'.format(rv_node, frame)

        #     # 1. Remove from .order property
        #     # 2. Put on the .redo property (to create property if not present)

    ############################################################
    # Business logic entry points
    ############################################################

    def create_session(self):
        if self.review_session.in_session:
            self.review_session.quit_review()

        def create_review_cb(url, error, data):
            if error == QtNetwork.QNetworkReply.NoError:
                if self.review_session.session_id:
                    self.analytics.log_amplitude_event(
                        "Session Created",
                        {"session_id": self.review_session.session_id},
                    )
                    self.session_creator = True
                self.join_session(self.review_session.session_id)

        self.review_session.create_review(callback=create_review_cb)

    def sync_playback(self):
        if self.use_legacy_schema:
            self.send_event(
                "mq-request-sync-playback",
                self.review_session.session_id,
                participant_hash=self.review_session.presenter_hash,
            )
        else:
            self.send_event(
                "GET", "", self.review_session.session_id, "PLAYBACK_SETTINGS"
            )

    def join_session(self, session_id):
        if not session_id:
            package_logger.error("Unable to connect to undefined channel")
            return

        session_id = session_id.upper().strip()

        if self.review_session.in_session:
            if self.review_session.session_id == session_id:
                return
            else:
                self.quit_review()

        def join_review_cb(url, error, data):
            if error == QtNetwork.QNetworkReply.NoError:
                if self.review_session.session_id:
                    self.analytics.log_amplitude_event(
                        "Session Joined", {"session_id": self.review_session.session_id}
                    )
                self.on_connection_succeeded()
            elif error == QtNetwork.QNetworkReply.NetworkError.ContentAccessDenied:
                self.analytics.log_amplitude_event(
                    "Join Session Failed", {"error": "Wrong Session Id"}
                )
                commands.sendInternalEvent(
                    "live-review-invalid-session-id", self.review_session.session_id
                )

        package_logger.info("Connecting to session: %s" % session_id)
        self.review_session.session_id = session_id
        self.review_session.join_review(callback=join_review_cb)

    def on_connection_succeeded(self):
        commands.defineModeMenu(self.menu_name.replace(" ", ""), self.menu, True)
        runtime.eval(
            """
                {
                    require live_review_sync;
                    live_review_sync.startUp();

                }
            """,
            ["live_review_sync"],
        )

        commands.sendInternalEvent(
            "internal-sync-session-id-changed", self.review_session.participant_hash
        )

        def fetch_participants_cb(url, error, data):
            if error == QtNetwork.QNetworkReply.NoError:
                if self.review_session.is_presenter:
                    # We are the presenter so we can process all events.
                    # If the presenter does not load any media before a new one gets
                    # elected, the first event that will be received will be the RV
                    # session from the new presenter, so no hole here.
                    self._can_process_graph_events = True
                else:
                    if self.use_legacy_schema:
                        self.send_event(
                            "mq-new-participants",
                            "",
                            self.review_session.presenter_hash,
                        )
                    else:
                        self.send_event(
                            "NEW_PARTICIPANTS",
                            "",
                            self.review_session.presenter_hash,
                            "LIVE_SESSION",
                        )

        self.review_session.fetch_participants(callback=fetch_participants_cb)
        package_logger.info("Sync session started")

    def quit_review(self):
        # Reset the event flags
        self._can_process_graph_events = False
        self._in_progressive_loading = False

        # don't block events for non-presenter anymore
        commands.setFilterLiveReviewEvents(False)

        # Stop the publisher's consumer before quiting the thread
        if self.review_session.in_session:
            self.review_session.quit_review(
                callback=lambda u, e, d: package_logger.info("Sync session shutdown")
            )

        if self._temp_file_name:
            os.remove(self._temp_file_name)
            self._temp_file_name = None

        commands.defineModeMenu(self.menu_name.replace(" ", ""), self.menu, True)

        runtime.eval(
            """
            {
                require live_review_sync;
                live_review_sync.shutdown();

            }
        """,
            ["sync"],
        )

        self._secret_manager.destroy_shared_key()
        # Remove the indicator since encryption is
        # no longer possible without a shared_key
        self.update_encryption_indicator_ui()

        if not self.session_creator:
            commands.clearSession()

    def send_event(
        self, event_name, payload="", participant_hash="", command="RV_EVENT"
    ):
        event_name = six.ensure_str(event_name)
        payload = six.ensure_str(payload)
        command = six.ensure_str(command)

        package_logger.debug(
            "Sending {} '{}' to {}".format(
                command, event_name, participant_hash or "everybody"
            )
        )

        # If it is a broadcast event and if we are not the current presenter,
        # avoid sending the event.
        if not self.review_session.is_presenter and participant_hash == "":
            package_logger.debug("Cannot broadcast {}".format(event_name))
            return

        # Discard all graph events if required.
        handle_event_key = (
            "{} {}".format(command, event_name) if command != "RV_EVENT" else event_name
        )
        is_live_review_event = handle_event_key in self.special_read_handler

        if not is_live_review_event and not self._can_send_graph_events:
            package_logger.debug("Cannot send graph event {}".format(event_name))
            return

        global_event = self.schema_parser.translate_to_global(
            event_name, payload, command
        )
        if self.use_legacy_schema:
            global_event = global_event[self.schema_parser.payload_field()]

        package_logger.debug(
            "send_event\n"
            + json.dumps(
                {
                    "RV": {
                        "event_name": event_name,
                        "command": command,
                        "payload": payload,
                    },
                },
                indent=2,
            )
        )

        self.review_session.send_event(json.dumps(global_event), participant_hash)

    def process_single_event(self, body):
        origin = six.ensure_str(body["SenderParticipantHash"])

        # Discard events that come from us.
        if origin == self.review_session.participant_hash:
            return

        if self.use_legacy_schema:
            event = self.schema_parser.translate_from_global(
                origin, self.schema_parser.wrap_payload(json.loads(body["Event"]))
            )
        else:
            event = self.schema_parser.translate_from_global(
                origin,
                self.schema_parser.wrap_payload(
                    json.loads(body["Event"])[self.schema_parser.payload_field()]
                ),
            )

        if event is None:
            return

        event_name, event_payload, event_command = event

        if event_name is None:
            return

        event_name = six.ensure_str(event_name)
        event_payload = six.ensure_str(event_payload)

        package_logger.debug(
            "process_single_event\n"
            + json.dumps(
                {
                    "RV": {
                        "event_name": event_name,
                        "command": event_command,
                        "payload": event_payload,
                    },
                },
                indent=2,
            )
        )

        package_logger.debug("Processing Event '{}'".format(event_name))

        handler_event_key = event_name
        is_live_review_event = handler_event_key in self.special_read_handler

        # Convert New Schema Command-Event Model for use in the handler dictionnary
        if event_command in _new_command_types and event_name in _new_event_types:
            handler_event_key = "{} {}".format(event_command, event_name)
            is_live_review_event = handler_event_key in self.special_read_handler

        # Discard all graph events if required.
        if not is_live_review_event and not self._can_process_graph_events:
            package_logger.debug("Discarding {}".format(event_name))
            return

        # Postpone events until progressive loading gets completed.
        if self._in_progressive_loading:
            self._post_progressive_load_callbacks.append(
                lambda: self.process_single_event(body)
            )
            return

        # Process the event.

        if is_live_review_event:
            self.special_read_handler[handler_event_key](origin, event_payload)
        else:
            try:
                # Filter out any events that are not from a remote participant
                # of the live review session.
                if event_name.startswith("remote-sync-"):
                    package_logger.debug(
                        "Processing {}: {} ".format(event_name, event_payload)
                    )
                    commands.sendInternalEvent(event_name, event_payload, origin)
                elif event_command in _new_command_types:
                    package_logger.debug(
                        "Processing New Schema {} {}: {} ".format(
                            event_command, event_name, event_payload
                        )
                    )

                    # Processing annotation events
                    if (
                        event_command in _new_command_types
                        and event_name in _annotation_event_types
                    ):
                        self.annotations_handler[event_name](origin, event_payload)
                    else:
                        package_logger.warning(
                            "Discarding unrecognized event: {} {}".format(
                                event_command, event_name
                            )
                        )
                else:
                    package_logger.warning(
                        "Discarding unrecognized event: {} {}".format(
                            event_command, event_name
                        )
                    )
            except:
                package_logger.error(
                    "Failed to execute received sync event: %s  Content: %s"
                    % (event_name, event_payload)
                )

    def on_request_timeout(self):
        if self.review_session.in_session:
            self.connectivity_manager.on_request_timeout()

    def on_request_failed(self):
        if self.review_session.in_session:
            self.connectivity_manager.on_request_failed()

    def on_new_latency_manager_ping(self, payload):
        if not self.review_session.in_session:
            return

        if self.review_session.is_presenter:
            self.send_event("mq-latency", payload)
        else:
            self.send_event("mq-latency", payload, self.review_session.presenter_hash)

    def on_service_broken_connection(self):
        # Cleanup the session before reporting the broken connection.
        self.quit_on_connection_error()
        self.connectivity_manager.on_service_broken_connection()

    def on_connection_issue_threshold(self):
        # Cleanup the session but keep the session ID.
        session_id = self.review_session.session_id
        self.quit_on_connection_error()
        self.review_session.session_id = session_id

    def quit_on_connection_error(self):
        if self.review_session.in_session:
            package_logger.error(
                "Connection Closed Unexpectedly. Disconnected from Sync Session."
            )
            extra_commands.displayFeedback(
                QtCore.QObject().tr("Connection broken..."), 5.0
            )

            self.quit_review()

    def on_new_presenter(self):
        commands.setFilterLiveReviewEvents(self.review_session.is_presenter is False)
        self.display_presenter()

        if self.review_session.is_presenter:
            self._can_send_graph_events = False
            if self.use_legacy_schema:
                self.send_event("mq-new-presenter", self.review_session.presenter_hash)
            else:
                self.send_event(
                    "NEW_PRESENTER",
                    self.review_session.presenter_hash,
                    command="LIVE_SESSION",
                )
            self.push_session_to_queue(self.get_current_session())
            self._can_send_graph_events = True

            commands.sendInternalEvent(
                "internal-sync-presenter-changed",
                self.serialized_participant_info(self.review_session.presenter_hash),
            )

    def request_sharedkey(self):
        package_logger.info(
            "Requesting shared key to presenter %s"
            % (self.review_session.presenter_hash)
        )
        pem = self._secret_manager.public_key_certificate()
        if self.use_legacy_schema:
            payload = six.ensure_str(base64.b64encode(pem))
        else:
            payload = six.ensure_str(pem)

        if self.use_legacy_schema:
            self.send_event(
                "mq-sharedkey-request",
                payload,
                self.review_session.presenter_hash,
            )
        else:
            self.send_event(
                "GET", payload, self.review_session.presenter_hash, "SHAREDKEY"
            )

    def ensure_sharedkey(self, skip_if_set=False):
        if not self.review_session.in_session:
            return

        if self._secret_manager.has_shared_key and not skip_if_set:
            return

        if self.review_session.is_presenter:
            self._secret_manager.generate_shared_key()
            self.update_encryption_indicator_ui()
        else:
            self.request_sharedkey()

    ############################################################
    # UX related functions
    ############################################################

    def open_review_panel(self, event=None):
        event and event.reject()
        package_logger.debug("LIVE REVIEW: Opening review panel")

        self.analytics.log_amplitude_event("Panel Toggled", {"intent": "Open Panel"})

        if self._reviewPaneDockWidget is None:
            sw = qtutils.sessionWindow()

            self._reviewPaneDockWidget = QtWidgets.QDockWidget("Live Review", sw)
            self._reviewPaneDockWidget.setObjectName("LiveReviewPanelDockWidget")
            self._reviewPaneDockWidget.setMinimumWidth(320)
            self._reviewPaneDockWidget.setMinimumHeight(1)

            sw.setTabPosition(QtCore.Qt.RightDockWidgetArea, QtWidgets.QTabWidget.North)
            sw.addDockWidget(QtCore.Qt.RightDockWidgetArea, self._reviewPaneDockWidget)

            self.tabify_overlapping_dock_widget()

            self._reviewPaneDockWidget.setWidget(self._reviewPaneWebView)
            self._reviewPaneDockWidget.setTitleBarWidget(QtWidgets.QWidget(None))

            self._connectivity_manager.start_checking_connection()
        else:
            if not (self._reviewPaneDockWidget.isVisible()):
                self._reviewPaneDockWidget.show()
                self.tabify_overlapping_dock_widget()
                self._connectivity_manager.start_checking_connection()

    def close_review_panel(self, event=None):
        event and event.reject()
        package_logger.error("LIVE REVIEW: Closing review panel")

        self.analytics.log_amplitude_event("Panel Toggled", {"intent": "Close Panel"})
        if self._reviewPaneDockWidget and self._reviewPaneDockWidget.isVisible():
            self._reviewPaneDockWidget.hide()
            self._connectivity_manager.stop_checking_connection()

    def toggle_review_panel(self, event=None):
        if self._reviewPaneDockWidget and self._reviewPaneDockWidget.isVisible():
            self.close_review_panel()
        else:
            self.open_review_panel()

    def override_double_arrow_menu(self):
        btb = qtutils.sessionBottomToolBar()
        actions = btb.actions()
        for action in actions:
            if action.toolTip() == QtCore.QObject().tr("Toggle RV Networking Dialog"):
                action.setToolTip(QtCore.QObject().tr("Toggle Live Review Panel"))
                action.triggered.disconnect()
                action.triggered.connect(self.toggle_review_panel)
                break

    def tabify_overlapping_dock_widget(self):
        """Tabify the review panel dock with overlapping dock widget if any"""
        # Find already existing QDockWidget in the right dock area if any
        # Note: Screening Room Details pane for example
        overlappingDockWidget = None
        sw = qtutils.sessionWindow()
        for dock in sw.findChildren(QtWidgets.QDockWidget):
            if dock == self._reviewPaneDockWidget:
                continue
            area = sw.dockWidgetArea(dock)
            if area == QtCore.Qt.RightDockWidgetArea:
                overlappingDockWidget = dock

        # Tabify with overlapping dock widget if any
        if overlappingDockWidget:
            sw.tabifyDockWidget(overlappingDockWidget, self._reviewPaneDockWidget)

    def update_encryption_indicator_ui(self):
        new_ui_state = self._secret_manager.has_shared_key
        if self._current_encryption_indicator_ui_state != new_ui_state:
            self._current_encryption_indicator_ui_state = new_ui_state
            commands.sendInternalEvent(
                "live-review-shared-key-changed",
                str(new_ui_state).lower(),
            )

    ############################################################
    # Helper functions
    ############################################################
    def display_presenter(self):
        if self.review_session.presenter_hash == "":
            return

        if self.review_session.is_presenter:
            extra_commands.displayFeedback(
                QtCore.QObject().tr("You are the Presenter"), 2.0
            )
            return

        presenter_name = self.get_participant_name(self.review_session.presenter_hash)
        if presenter_name == "unknown":

            def fetch_participants_cb(url, error, data):
                if error == QtNetwork.QNetworkReply.NoError:
                    self.display_presenter()

            self.review_session.fetch_participants(callback=fetch_participants_cb)
            return

        extra_commands.displayFeedback(
            presenter_name + " " + QtCore.QObject().tr("is the Presenter"), 2.0
        )

    def push_session_to_queue(self, session_info, participant_hash=""):
        try:
            fps = commands.getFloatProperty(six.ensure_str("#RVRetime.output.fps"))[0]
        except:
            fps = "24"

        payload = "%s;;%s;;%s;;%s" % (
            fps,
            str(commands.frame()),
            str(commands.playMode()),
            session_info,
        )
        self.send_event("mq-session", payload, participant_hash=participant_hash)

    def get_participant_name(self, participant_hash):
        """Returns the participant's name for the participant hash specified"""

        participant_name = "unknown"
        for p in self.review_session.participants:
            p_hash = p.get("ParticipantHash", "")
            if p_hash == participant_hash:
                p_info = p.get("ParticipantInfo", "{}")

                participant_name = json.loads(p_info).get("name", "???")
                break

        return participant_name

    def get_current_session(self):
        # Whether the name can be used to open the file a second time,
        # while the named temporary file is still open, varies across
        # platforms (it can be so used on Unix; it cannot on Windows NT
        # or later)
        with tempfile.NamedTemporaryFile(
            prefix="LiveReview-", suffix=".rv", mode="wb", delete=False
        ) as temp_file:
            file_name = temp_file.name.replace("\\", "/")
        commands.saveSession(file_name, True, True, False)

        with open(file_name, "rb") as sessionFile:
            session_info = [c for c in bytearray(sessionFile.read())]

        os.remove(file_name)

        return json.dumps(session_info)

    def compare_with_current_session(self, target_file_name):
        """Returns true if the target session is equal to the current session, false otherwise"""
        # Save current session to a file
        # Whether the name can be used to open the file a second time,
        # while the named temporary file is still open, varies across
        # platforms (it can be so used on Unix; it cannot on Windows NT
        # or later)
        with tempfile.NamedTemporaryFile(
            prefix="LiveReview-", suffix=".rv", mode="wb", delete=False
        ) as temp_file:
            current_file_name = temp_file.name.replace("\\", "/")
            commands.saveSession(current_file_name, True, True, False)

        # First assume that they are equal
        same = True

        class NotTheSame(Exception):
            """
            Custom exception to indicate that the sessions differ
            """

        def compare_floats(new, cur):
            package_logger.debug(
                "compare_floats: Comparing {} with {}".format(str(new), str(cur))
            )

            if type(new) in [tuple, list]:
                if len(new) != len(cur):
                    return False

                for i in range(len(new)):
                    if not compare_floats(new[i], cur[i]):
                        return False

            elif type(new) is float:
                if not math_isclose(new, cur, rel_tol=1e-5):
                    return False

            else:
                package_logger.debug(
                    "compare_floats: Unsupported type {}".format(str(type(new)))
                )
                return False

            return True

        # Compare both gto sessions
        try:
            # Read both gto sessions
            cur_session = gc.gtoContainer(six.ensure_binary(current_file_name), False)
            new_session = gc.gtoContainer(six.ensure_binary(target_file_name), False)

            # Validate that they both have the same number of objects to begin with
            if len(cur_session.objects()) != len(new_session.objects()):
                raise NotTheSame(
                    "Number of objects differ: new session={}, current session={}".format(
                        len(new_session.objects()), len(cur_session.objects())
                    )
                )

            # Validate that they have the same property values
            for new_object in new_session.objects():
                for new_component in new_object.components():
                    for new_property in new_component.properties():
                        new_property_data = new_property.data()
                        cur_property_data = cur_session[new_object.name()][
                            new_component.name()
                        ][new_property.name()].data()
                        if new_property_data != cur_property_data:
                            # Ignore currentFrame as it is set anyway
                            if (
                                (new_property.name() == "currentFrame")
                                and (new_component.name() == "session")
                                and (new_object.name() == "rv")
                            ):
                                pass

                            # Note that the RV session properties are also copied in the view node
                            elif (new_property.name() == "frame") and (
                                new_component.name() == "session"
                            ):
                                pass

                            # Ignore differences in float values due to precision loss
                            elif compare_floats(new_property_data, cur_property_data):
                                pass

                            # Otherwise raise a difference
                            else:
                                raise NotTheSame(
                                    "New session {}/{}/{} property value={} is different from current session value={}".format(
                                        new_object.name(),
                                        new_component.name(),
                                        new_property.name(),
                                        new_property_data,
                                        cur_property_data,
                                    )
                                )

        except NotTheSame as e:
            package_logger.debug("Sessions differ: {}".format(str(e)))
            same = False
        except Exception as e:
            package_logger.debug("Sessions differ: {}".format(str(e)))
            same = False

        os.remove(current_file_name)

        return same

    def is_encrypted_event(self, event_name):
        # diag-info are sent in clear for debug
        # shared-key events can't be encrypted using the same encryption
        # as other events since the shared-key needed for encryption is
        # not set from either the sender or the receiver
        ignore_list = [
            "mq-diag-info-request",
            "mq-diag-info-event",
            "mq-diag-info-response",
            "mq-sharedkey-request",
            "mq-sharedkey-response",
            "mq-new-presenter",
            "mq-new-participants",
            "SHAREDKEY GET",
            "SHAREDKEY SET",
            "LIVE_SESSION NEW_PRESENTER",
            "LIVE_SESSION NEW_PARTICIPANTS",
        ]
        return event_name not in ignore_list

    def serialized_participant_info(self, participant_hash):
        return "%s||%s" % (
            participant_hash,
            self.get_participant_name(participant_hash),
        )


def createMode():
    return LiveReviewMode()
