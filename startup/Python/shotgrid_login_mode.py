#
# Copyright (C) 2023  Autodesk, Inc. All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0
#

import os
from urllib.parse import urlparse

from PySide2 import QtCore
from PySide2.QtNetwork import QNetworkCookie
from PySide2.QtWebEngineWidgets import QWebEngineProfile
from rv import commands, rvtypes, extra_commands
from rv.commands import NeutralMenuState, DisabledMenuState, CheckedMenuState

os.environ["TK_AUTH_PRODUCT"] = QtCore.QCoreApplication.applicationName()

if "SGTK_DEFAULT_AUTH_METHOD" not in os.environ:
    os.environ["SGTK_DEFAULT_AUTH_METHOD"] = "app_session_launcher"

if "RV_SHOTGUN_AUTH_NO_HTTPS" in os.environ:
    os.environ["SGTK_AUTH_ALLOW_NO_HTTPS"] = "1"

login_required = True

default_shotgrid_host = os.environ.get(
    "RV_SHOTGRID_DEFAULT_SERVER_URL",
    os.environ.get("RV_SHOTGUN_DEFAULT_SERVER_URL", None),
)
fixed_shotgrid_host = os.environ.get(
    "RV_SHOTGRID_FIXED_SERVER_URL", os.environ.get("RV_SHOTGUN_FIXED_SERVER_URL", None)
)


class ShotGridLogin(rvtypes.MinorMode, QtCore.QObject):
    def __init__(self):
        from sgtk.authentication import (
            set_shotgun_authenticator_support_web_login,
        )

        set_shotgun_authenticator_support_web_login(True)

        rvtypes.MinorMode.__init__(self)
        QtCore.QObject.__init__(self, QtCore.QCoreApplication.instance())

        self.init(self.name, self.global_bindings, self.local_bindings, self.menu)

    ############################################################
    # Global Properties
    ############################################################

    @property
    def name(self):
        return self.__class__.__name__

    @property
    def local_bindings(self):
        return None

    @property
    def global_bindings(self):
        return [
            (
                "session-initialized",
                self.ensure_login,
                "Login to ShotGrid, if not already logged in",
            ),
        ]

    @property
    def menu(self):
        return [
            (
                QtCore.QObject().tr("Locksmith"),
                [
                    (
                        QtCore.QObject().tr("ShotGrid Connection Details"),
                        list(
                            self.get_user_menu_items()
                            or self.get_default_menu_items()
                            or []
                        )
                        + [
                            ("_", None),
                            (
                                QtCore.QObject().tr("New ShotGrid Connection Details"),
                                self.new_shotgrid_connection_details,
                                None,
                                lambda: NeutralMenuState,
                            ),
                            (
                                QtCore.QObject().tr(
                                    "Clear All ShotGrid Connection Details"
                                ),
                                self.clear_all_shotgrid_connection_details,
                                None,
                                lambda: NeutralMenuState,
                            ),
                        ],
                    ),
                ],
            )
        ]

    @property
    def current_user(self):
        import sgtk

        return sgtk.get_authenticated_user()

    @current_user.setter
    def current_user(self, user):
        if self.current_user == user or (
            self.current_host == getattr(user, "host", "")
            and self.current_login == getattr(user, "login", "")
        ):
            return

        try:
            import sgtk

            sgtk.set_authenticated_user(user)

            if self.current_user:
                self.session_cache.set_current_host(self.current_host)
                self.session_cache.set_current_user(
                    self.current_host, self.current_login
                )

                default_profile = QWebEngineProfile.defaultProfile()
                cookie_store = default_profile.cookieStore()
                cookie_store.setCookie(
                    QNetworkCookie(
                        b"_session_id", self.current_session_id.encode("utf-8")
                    ),
                    self.current_host,
                )

                self.execute_later(
                    lambda: commands.sendInternalEvent(
                        "sgtk-authenticated-user-changed"
                    )
                )
            else:
                self.execute_later(
                    lambda: commands.sendInternalEvent("sgtk-connection-cleared-out")
                )
        finally:
            commands.defineModeMenu(self.name, self.menu, True)

    @property
    def current_host(self):
        return self.current_user.host if self.current_user else ""

    @property
    def current_login(self):
        return self.current_user.login if self.current_user else ""

    @property
    def current_session_id(self):
        return self.current_user.impl.get_session_token() if self.current_user else ""

    @property
    def shotgun_authenticator(self):
        from sgtk.authentication import ShotgunAuthenticator

        return ShotgunAuthenticator()

    @property
    def session_cache(self):
        from sgtk.authentication import session_cache

        return session_cache

    ############################################################
    # Menu items implementation
    ############################################################

    def new_shotgrid_connection_details(self, event=None):
        self.login(prompt=True)

    def clear_all_shotgrid_connection_details(self, event=None):
        for host in self.session_cache.get_recent_hosts():
            for login in self.session_cache.get_recent_users(host):
                self.session_cache.delete_session_data(host, login)

        self.shotgun_authenticator.clear_default_user()
        self.current_user = None

        self.login(prompt=True)

    def switch_shotgrid_user(self, host, login, event=None):
        if (
            urlparse(self.current_host).netloc == urlparse(host).netloc
            and self.current_login == login
        ):
            return self.current_user

        extra_commands.displayFeedback(
            QtCore.QObject().tr("Switching ShotGrid User..."), 3.0
        )

        try:
            new_user = self.shotgun_authenticator.create_session_user(
                login=login, host=host
            )
        except:
            new_user = None

        return self.login(as_user=new_user, fixed_host=host)

    ############################################################
    # Helper Functions
    ############################################################

    def execute_later(self, func):
        timer = QtCore.QTimer(self)
        timer.setInterval(1000)
        timer.timeout.connect(func)
        timer.timeout.connect(lambda: timer.deleteLater())
        timer.setSingleShot(True)
        timer.start()

    def get_user_menu_items(self):
        if self.current_user:
            yield self.create_user_menu_item(
                self.current_host, self.current_login, CheckedMenuState
            )

        for host in self.session_cache.get_recent_hosts():
            for login in self.session_cache.get_recent_users(host):
                if host == self.current_host and login == self.current_login:
                    continue

                if self.session_cache.get_session_data(host, login):
                    yield self.create_user_menu_item(host, login, NeutralMenuState)

    def get_default_menu_items(self):
        return [
            (
                QtCore.QObject().tr("No ShotGrid Connection Details"),
                None,
                None,
                lambda: DisabledMenuState,
            )
        ]

    def create_submenu(self, user_menu_items):
        return [
            (
                QtCore.QObject().tr("ShotGrid Connection Details"),
                list(user_menu_items)
                + [
                    ("_", None),
                    (
                        QtCore.QObject().tr("New ShotGrid Connection Details"),
                        self.new_shotgrid_connection_details,
                        None,
                        lambda: NeutralMenuState,
                    ),
                    (
                        QtCore.QObject().tr("Clear All ShotGrid Connection Details"),
                        self.clear_all_shotgrid_connection_details,
                        None,
                        lambda: NeutralMenuState,
                    ),
                ],
            ),
        ]

    def create_user_menu_item(self, host, login, menu_state):
        return (
            QtCore.QObject().tr(f"{urlparse(host).netloc} ({login})"),
            lambda event, host=host, login=login: self.switch_shotgrid_user(
                host, login, event
            ),
            None,
            lambda: menu_state,
        )

    def ensure_login(self, event=None):
        if event:
            event.reject()

        global login_required
        if login_required:
            self.login()

    def login(self, prompt=False, fixed_host=fixed_shotgrid_host, as_user=None):
        global login_required
        login_required = False

        try:
            if as_user:
                user = as_user
                try:
                    user.refresh_credentials()
                except:
                    if fixed_host:
                        return self.login(prompt=True, fixed_host=fixed_host)
                    else:
                        raise

            elif prompt or fixed_host:
                user = self.shotgun_authenticator.get_user_from_prompt(
                    is_host_fixed=bool(fixed_host),
                    host=fixed_host or default_shotgrid_host,
                )

            else:
                try:
                    # Try to retrieve a user from the defaults manager/session cache
                    user = self.shotgun_authenticator.get_default_user()
                    if user:
                        user.refresh_credentials()
                    else:
                        raise RuntimeError("No user found")
                except:
                    return self.login(prompt=True, fixed_host=fixed_host)

            self.current_user = user

        except Exception as e:
            print(e)
            return None

        return user


the_mode = None


def createMode():
    global the_mode
    the_mode = ShotGridLogin()
    return the_mode


def theMode():
    return the_mode
