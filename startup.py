# Copyright (c) 2013 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

import os
import sys

import sgtk
from sgtk.platform import SoftwareLauncher, SoftwareVersion, LaunchInformation


class RvLauncher(SoftwareLauncher):
    """
    Handles launching RV executables. Automatically starts up
    a tk-rv engine with the current context in the new session
    of RV.
    """

    @property
    def minimum_supported_version(self):
        """
        The minimum software version that is supported by the launcher.
        """
        return "7.0"

    def prepare_launch(self, exec_path, args, file_to_open=None):
        """
        Prepares an environment to launch RV in that will automatically
        load Toolkit and the tk-rv engine when RV starts.

        :param str exec_path: Path to RV executable to launch.
        :param str args: Command line arguments as strings.
        :param str file_to_open: (optional) Full path name of a file to open on
                                 launch.
        :returns: :class:`LaunchInformation` instance
        """
        required_env = {}

        required_env["SGTK_ENGINE"] = self.engine_name
        required_env["RV_SUPPORT_PATH"] = os.path.join(self.disk_location, "startup")
        required_env["RV_PREFS_CLOBBER_PATH"] = os.path.join(
            self.disk_location, "startup"
        )
        required_env["TK_DEBUG"] = os.environ.get("TK_DEBUG") and "true" or ""

        if file_to_open:
            # Add the file name to open to the launch environment
            required_env["SGTK_FILE_TO_OPEN"] = file_to_open

        std_env = self.get_standard_plugin_environment()
        required_env.update(std_env)

        return LaunchInformation(exec_path, args, required_env)
