# Copyright (C) 2011-2015 2ndQuadrant Italia (Devise.IT S.r.L.)
#
# This file is part of Barman.
#
# Barman is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Barman is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Barman.  If not, see <http://www.gnu.org/licenses/>.

"""
This module represents a backup.
"""

from glob import glob
import datetime
from io import StringIO
import logging
import os
import shutil
import time
import tempfile
import re

import dateutil.parser
import dateutil.tz

from barman.infofile import WalFileInfo, BackupInfo, UnknownBackupIdException
from barman.fs import UnixLocalCommand, UnixRemoteCommand, FsOperationFailed
from barman import xlog, output
from barman.command_wrappers import Rsync, RsyncPgData, \
    CommandFailedException, DataTransferFailure
from barman.compression import CompressionManager, CompressionIncompatibility
from barman.hooks import HookScriptRunner
from barman.utils import human_readable_timedelta, mkpath, pretty_size, \
    fsync_dir
from barman.config import BackupOptions
from barman.backup_executor import RsyncBackupExecutor


_logger = logging.getLogger(__name__)


class BackupManager(object):
    """Manager of the backup archive for a server"""

    DEFAULT_STATUS_FILTER = (BackupInfo.DONE,)
    DANGEROUS_OPTIONS = ['data_directory', 'config_file', 'hba_file',
            'ident_file', 'external_pid_file', 'ssl_cert_file',
            'ssl_key_file', 'ssl_ca_file', 'ssl_crl_file',
            'unix_socket_directory']

    def __init__(self, server):
        """
        Constructor
        """
        self.name = "default"
        self.server = server
        self.config = server.config
        self._backup_cache = None
        self.compression_manager = CompressionManager(self.config)
        self.executor = RsyncBackupExecutor(self)

    def get_available_backups(self, status_filter=DEFAULT_STATUS_FILTER):
        """
        Get a list of available backups

        :param status_filter: default DEFAULT_STATUS_FILTER. The status of
            the backup list returned
        """
        # If the filter is not a tuple, create a tuple using the filter
        if not isinstance(status_filter, tuple):
            status_filter = tuple(status_filter,)
        # Load the cache if necessary
        if self._backup_cache is None:
            self._load_backup_cache()
        # Filter the cache using the status filter tuple
        backups = {}
        for key, value in self._backup_cache.iteritems():
            if value.status in status_filter:
                backups[key] = value
        return backups

    def _load_backup_cache(self):
        """
        Populate the cache of the available backups, reading information
        from disk.
        """
        self._backup_cache = {}
        # Load all the backups from disk reading the backup.info files
        for filename in glob("%s/*/backup.info" %
                             self.config.basebackups_directory):
            backup = BackupInfo(self.server, filename)
            self._backup_cache[backup.backup_id] = backup

    def backup_cache_add(self, backup_info):
        """
        Register a BackupInfo object to the backup cache.

        NOTE: Initialise the cache - in case it has not been done yet

        :param barman.infofile.BackupInfo backup_info: the object we want to
            register in the cache
        """
        # Load the cache if needed
        if self._backup_cache is None:
            self._load_backup_cache()
        # Insert the BackupInfo object into the cache
        self._backup_cache[backup_info.backup_id] = backup_info

    def backup_cache_remove(self, backup_info):
        """
        Remove a BackupInfo object from the backup cache

        This method _must_ be called after removing the object from disk.

        :param barman.infofile.BackupInfo backup_info: the object we want to
            remove from the cache
        """
        # Nothing to do if the cache is not loaded
        if self._backup_cache is None:
            return
        # Remove the BackupInfo object from the backups cache
        del self._backup_cache[backup_info.backup_id]

    def get_backup(self, backup_id):
        """
        Return the backup information for the given backup id.

        If the backup_id is None or backup.info file doesn't exists,
        it returns None.

        :param str|None backup_id: the ID of the backup to return
        :rtype: BackupInfo|None
        """
        if backup_id is not None:
            # Get all the available backups from the cache
            available_backups = self.get_available_backups(
                BackupInfo.STATUS_ALL)
            # Return the BackupInfo if present, or None
            return available_backups.get(backup_id)
        return None

    def get_previous_backup(self, backup_id, status_filter=DEFAULT_STATUS_FILTER):
        """
        Get the previous backup (if any) in the catalog

        :param status_filter: default DEFAULT_STATUS_FILTER. The status of
            the backup returned
        """
        if not isinstance(status_filter, tuple):
            status_filter = tuple(status_filter)
        backup = BackupInfo(self.server, backup_id=backup_id)
        available_backups = self.get_available_backups(status_filter +
                                                       (backup.status,))
        ids = sorted(available_backups.keys())
        try:
            current = ids.index(backup_id)
            while current > 0:
                res = available_backups[ids[current - 1]]
                if res.status in status_filter:
                    return res
                current -= 1
            return None
        except ValueError:
            raise UnknownBackupIdException('Could not find backup_id %s' %
                                           backup_id)

    def get_next_backup(self, backup_id, status_filter=DEFAULT_STATUS_FILTER):
        """
        Get the next backup (if any) in the catalog

        :param status_filter: default DEFAULT_STATUS_FILTER. The status of
            the backup returned
        """
        if not isinstance(status_filter, tuple):
            status_filter = tuple(status_filter)
        backup = BackupInfo(self.server, backup_id=backup_id)
        available_backups = self.get_available_backups(status_filter +
                                                       (backup.status,))
        ids = sorted(available_backups.keys())
        try:
            current = ids.index(backup_id)
            while current < (len(ids) - 1):
                res = available_backups[ids[current + 1]]
                if res.status in status_filter:
                    return res
                current += 1
            return None
        except ValueError:
            raise UnknownBackupIdException('Could not find backup_id %s' % backup_id)

    def get_last_backup(self, status_filter=DEFAULT_STATUS_FILTER):
        """
        Get the last backup (if any) in the catalog

        :param status_filter: default DEFAULT_STATUS_FILTER. The status of the backup returned
        """
        available_backups = self.get_available_backups(status_filter)
        if len(available_backups) == 0:
            return None

        ids = sorted(available_backups.keys())
        return ids[-1]

    def get_first_backup(self, status_filter=DEFAULT_STATUS_FILTER):
        """
        Get the first backup (if any) in the catalog

        :param status_filter: default DEFAULT_STATUS_FILTER. The status of the backup returned
        """
        available_backups = self.get_available_backups(status_filter)
        if len(available_backups) == 0:
            return None

        ids = sorted(available_backups.keys())
        return ids[0]

    def delete_backup(self, backup):
        """
        Delete a backup

        :param backup: the backup to delete
        """
        available_backups = self.get_available_backups()
        # Honour minimum required redundancy
        if backup.status == BackupInfo.DONE and \
                self.server.config.minimum_redundancy >= len(available_backups):
            output.warning("Skipping delete of backup %s for server %s due to "
                           "minimum redundancy requirements "
                           "(minimum redundancy = %s, current redundancy = %s)",
                           backup.backup_id,
                           self.config.name,
                           len(available_backups),
                           self.server.config.minimum_redundancy)
            return

        output.info("Deleting backup %s for server %s",
                    backup.backup_id, self.config.name)
        previous_backup = self.get_previous_backup(backup.backup_id)
        next_backup = self.get_next_backup(backup.backup_id)
        # Delete all the data contained in the backup
        try:
            self.delete_backup_data(backup)
        except OSError as e:
            output.error("Failure deleting backup %s for server %s.\n%s",
                         backup.backup_id, self.config.name, e)
            return
        # Check if we are deleting the first available backup
        if not previous_backup:
            # In the case of exclusive backup (default), removes any WAL
            # files associated to the backup being deleted.
            # In the case of concurrent backup, removes only WAL files
            # prior to the start of the backup being deleted, as they
            # might be useful to any concurrent backup started immediately
            # after.
            remove_until = None  # means to remove all WAL files
            if next_backup:
                remove_until = next_backup
            elif BackupOptions.CONCURRENT_BACKUP in self.config.backup_options:
                remove_until = backup
            output.info("Delete associated WAL segments:")
            for name in self.remove_wal_before_backup(remove_until):
                output.info("\t%s", name)
        # As last action, remove the backup directory,
        # ending the delete operation
        try:
            self.delete_basebackup(backup)
        except OSError as e:
            output.error("Failure deleting backup %s for server %s.\n%s\n"
                         "Please manually remove the '%s' directory",
                         backup.backup_id, self.config.name, e,
                         backup.get_basebackup_directory())
            return
        self.backup_cache_remove(backup)
        output.info("Done")

    def retry_backup_copy(self, target_function, *args, **kwargs):
        """
        Execute the copy of a base backup, retrying a given number of times

        :param target_function: the base backup copy function
        :param args: args for the copy function
        :param kwargs: kwargs of the copy function
        :return: the result of the copy function
        """
        attempts = 0
        while True:
            try:
                # if is not the first attempt, output the retry number
                if attempts >= 1:
                    output.warning("Copy of base backup: retry #%s", attempts)
                return target_function(*args, **kwargs)
            # catch rsync errors
            except DataTransferFailure, e:
                # exit condition: if retry number is lower than configured retry
                # limit, try again; otherwise exit.
                if attempts < self.config.basebackup_retry_times:
                    # Log the exception, for debugging purpose
                    _logger.exception("Failure in base backup copy: %s", e)
                    output.warning(
                        "Copy of base backup failed, waiting for next "
                        "attempt in %s seconds",
                        self.config.basebackup_retry_sleep)
                    # sleep for configured time. then try again
                    time.sleep(self.config.basebackup_retry_sleep)
                    attempts += 1
                else:
                    # if the max number of attempts is reached an there is still
                    # an error, exit re-raising the exception.
                    raise

    def backup(self):
        """
        Performs a backup for the server
        """
        _logger.debug("initialising backup information")
        self.executor.init()
        backup_info = None
        try:
            # Create the BackupInfo object representing the backup
            backup_info = BackupInfo(
                self.server,
                backup_id=datetime.datetime.now().strftime('%Y%m%dT%H%M%S'))
            backup_info.save()
            self.backup_cache_add(backup_info)
            output.info(
                "Starting backup for server %s in %s",
                self.config.name,
                backup_info.get_basebackup_directory())

            # Run the pre-backup-script if present.
            script = HookScriptRunner(self, 'backup_script', 'pre')
            script.env_from_backup_info(backup_info)
            script.run()

            # Do the backup using the BackupExecutor
            self.executor.backup(backup_info)

            # Compute backup size and fsync it on disk
            self.backup_fsync_and_set_sizes(backup_info)

            # Mark the backup as DONE
            backup_info.set_attribute("status", "DONE")
        # Use BaseException instead of Exception to catch events like
        # KeyboardInterrupt (e.g.: CRTL-C)
        except BaseException, e:
            msg_lines = str(e).strip().splitlines()
            if backup_info:
                # Use only the first line of exception message
                # in backup_info error field
                backup_info.set_attribute("status", "FAILED")
                # If the exception has no attached message use the raw type name
                if len(msg_lines) == 0:
                    msg_lines = [type(e).__name__]
                backup_info.set_attribute(
                    "error",
                    "failure %s (%s)" % (
                        self.executor.current_action, msg_lines[0]))

            output.error("Backup failed %s.\nDETAILS: %s\n%s",
                         self.executor.current_action, msg_lines[0],
                         '\n'.join(msg_lines[1:]))

        else:
            output.info("Backup end at xlog location: %s (%s, %08X)",
                        backup_info.end_xlog,
                        backup_info.end_wal,
                        backup_info.end_offset)
            output.info("Backup completed")
        finally:
            if backup_info:
                backup_info.save()

                # Run the post-backup-script if present.
                script = HookScriptRunner(self, 'backup_script', 'post')
                script.env_from_backup_info(backup_info)
                script.run()

        output.result('backup', backup_info)

    def recover(self, backup_info, dest, tablespaces=None, target_tli=None,
                target_time=None, target_xid=None, target_name=None,
                exclusive=False, remote_command=None):
        """
        Performs a recovery of a backup

        :param barman.infofile.BackupInfo backup_info: the backup to recover
        :param str dest: the destination directory
        :param dict[str,str]|None tablespaces: a tablespace name -> location map
            (for relocation)
        :param str|None target_tli: the target timeline
        :param str|None target_time: the target time
        :param str|None target_xid: the target xid
        :param str|None target_name: the target name created previously with
                            pg_create_restore_point() function call
        :param bool exclusive: whether the recovery is exclusive or not
        :param str|None remote_command: default None. The remote command to recover
                               the base backup, in case of remote backup.
        """

        # run the cron to be sure the wal catalog is up to date
        self.server.cron(verbose=False)

        recovery_dest = 'local'

        if remote_command:
            recovery_dest = 'remote'
            rsync = RsyncPgData(
                ssh=remote_command,
                bwlimit=self.config.bandwidth_limit,
                network_compression=self.config.network_compression)
            try:
                # create a UnixRemoteCommand obj if is a remote recovery
                cmd = UnixRemoteCommand(remote_command)
            except FsOperationFailed:
                output.error(
                    "Unable to connect to the target host using the command "
                    "'%s'", remote_command)
                output.close_and_exit()
        else:
            # if is a local recovery create a UnixLocalCommand
            cmd = UnixLocalCommand()
            # silencing static analysis tools
            rsync = None
        output.info("Starting %s restore for server %s using backup %s ",
                    recovery_dest, self.config.name, backup_info.backup_id)

        # check destination directory. If doesn't exist create it
        try:
            cmd.create_dir_if_not_exists(dest)
        except FsOperationFailed, e:
            output.exception("unable to initialize destination directory "
                             "'%s': %s", dest, e)
            output.close_and_exit()

        output.info("Destination directory: %s", dest)

        # initialize tablespace structure
        if backup_info.tablespaces:
            tblspc_dir = os.path.join(dest, 'pg_tblspc')
            try:
                # check for pg_tblspc dir into recovery destination folder.
                # if does not exists, create it
                cmd.create_dir_if_not_exists(tblspc_dir)
            except FsOperationFailed, e:
                output.exception("unable to initialize tablespace directory "
                                 "'%s': %s", tblspc_dir, e)
                output.close_and_exit()

            for item in backup_info.tablespaces:

                # build the filename of the link under pg_tblspc directory
                pg_tblspc_file = os.path.join(tblspc_dir, str(item.oid))

                # by default a tablespace goes in the same location where
                # it was on the source server when the backup was taken
                location = item.location

                # if a relocation has been requested for this tablespace
                # use the user provided target directory
                if tablespaces and item.name in tablespaces:
                    location = tablespaces[item.name]

                try:
                    # remove the current link in pg_tblspc if exists
                    # (raise if it's a directory)
                    cmd.delete_if_exists(pg_tblspc_file)
                    # create tablespace location if not exists
                    # (raise if not possible)
                    cmd.create_dir_if_not_exists(location)
                    # check for write permission into destination directory
                    cmd.check_write_permission(location)
                    # create symlink between tablespace and recovery folder
                    cmd.create_symbolic_link(location, pg_tblspc_file)
                except FsOperationFailed, e:
                    output.exception("unable to prepare '%s' tablespace "
                                     "(destination '%s'): %s",
                                     item.name, location, e)
                    output.close_and_exit()

                output.info("\t%s, %s, %s", item.oid, item.name, location)

        wal_dest = os.path.join(dest, 'pg_xlog')
        target_epoch = None
        target_datetime = None
        if target_time:
            # noinspection PyBroadException
            try:
                target_datetime = dateutil.parser.parse(target_time)
            except ValueError as e:
                output.exception("unable to parse the target time parameter "
                                 "%r: %s", target_time, e)
                output.close_and_exit()
            except Exception:
                # this should not happen, but there is a known bug in
                # dateutil.parser.parse() implementation
                # ref: https://bugs.launchpad.net/dateutil/+bug/1247643
                output.exception("unable to parse the target time parameter "
                                 "%r", target_time)
                output.close_and_exit()

            target_epoch = time.mktime(target_datetime.timetuple()) + (
                target_datetime.microsecond / 1000000.)
        if target_time or target_xid or (
                target_tli and
                target_tli != backup_info.timeline) or target_name:
            targets = {}
            if target_time:
                targets['time'] = str(target_datetime)
            if target_xid:
                targets['xid'] = str(target_xid)
            if target_tli and target_tli != backup_info.timeline:
                targets['timeline'] = str(target_tli)
            if target_name:
                targets['name'] = str(target_name)
            output.info(
                "Doing PITR. Recovery target %s",
                (", ".join(["%s: %r" % (k, v) for k, v in targets.items()])))
            wal_dest = os.path.join(dest, 'barman_xlog')

        # Retrieve the safe_horizon for smart copy
        # If the target directory contains a previous recovery, it is safe to
        # pick the least of the two backup "begin times" (the one we are
        # recovering now and the one previously recovered in the target
        # directory)
        #
        # noinspection PyBroadException
        try:
            backup_begin_time = backup_info.begin_time
            # Retrieve previously recovered backup metadata (if available)
            dest_info_txt = cmd.get_file_content(
                os.path.join(dest, '.barman-recover.info'))
            dest_info = BackupInfo(
                self.server,
                info_file=StringIO(dest_info_txt))
            dest_begin_time = dest_info.begin_time
            # Pick the earlier begin time. Both are tz-aware timestamps because
            # BackupInfo class ensure it
            safe_horizon = min(backup_begin_time, dest_begin_time)
            output.info("Using safe horizon time for smart rsync copy: %s",
                        safe_horizon)
        except FsOperationFailed, e:
            # Setting safe_horizon to None will effectively disable
            # the time-based part of smart_copy method. However it is still
            # faster than running all the transfers with checksum enabled.
            #
            # FsOperationFailed means the .barman-recover.info is not available
            # on destination directory
            safe_horizon = None
            _logger.warning('Unable to retrieve safe horizon time '
                            'for smart rsync copy: %s', e)
        except Exception, e:
            # Same as above, but something failed decoding .barman-recover.info
            # or comparing times, so log the full traceback
            safe_horizon = None
            _logger.exception('Error retrieving safe horizon time '
                              'for smart rsync copy: %s', e)

        # Copy the base backup
        output.info("Copying the base backup.")
        try:
            # perform the backup copy, honoring the retry option if set
            self.retry_backup_copy(self.recover_basebackup_copy, backup_info,
                                   dest, tablespaces, remote_command,
                                   safe_horizon)
        except DataTransferFailure, e:
            output.exception("Failure copying base backup: %s", e)
            output.close_and_exit()

        # Prepare WAL segments local directory
        output.info("Copying required wal segments.")

        # Retrieve the list of required WAL segments
        # according to recovery options
        xlogs = {}
        required_xlog_files = tuple(
            self.server.get_required_xlog_files(backup_info, target_tli,
                                                target_epoch))
        for wal_info in required_xlog_files:
            hashdir = xlog.hash_dir(wal_info.name)
            if hashdir not in xlogs:
                xlogs[hashdir] = []
            xlogs[hashdir].append(wal_info.name)
        # Check decompression options
        compressor = self.compression_manager.get_compressor()

        # Restore WAL segments
        try:
            self.recover_xlog_copy(compressor, xlogs, wal_dest, remote_command)
        except DataTransferFailure, e:
            output.exception("Failure copying WAL files: %s", e)
            output.close_and_exit()

        # Generate recovery.conf file (only if needed by PITR)
        if target_time or target_xid or (
                target_tli and target_tli != backup_info.timeline) or \
                target_name:
            output.info("Generating recovery.conf")
            if remote_command:
                tempdir = tempfile.mkdtemp(prefix='barman_recovery-')
                recovery = open(os.path.join(tempdir, 'recovery.conf'), 'w')
            else:
                recovery = open(os.path.join(dest, 'recovery.conf'), 'w')
            print >> recovery, "restore_command = 'cp barman_xlog/%f %p'"
            if backup_info.version >= 80400:
                print >> recovery, "recovery_end_command = 'rm -fr barman_xlog'"
            if target_time:
                print >> recovery, "recovery_target_time = '%s'" % target_time
            if target_tli:
                print >> recovery, "recovery_target_timeline = %s" % target_tli
            if target_xid:
                print >> recovery, "recovery_target_xid = '%s'" % target_xid
            if target_name:
                print >> recovery, "recovery_target_name = '%s'" % target_name
            if (target_xid or target_time) and exclusive:
                print >> recovery, "recovery_target_inclusive = '%s'" % (
                    not exclusive)
            recovery.close()
            if remote_command:
                # Uses plain rsync (without exclusions) to ship recovery.conf
                plain_rsync = Rsync(
                        ssh=remote_command,
                        bwlimit=self.config.bandwidth_limit,
                        network_compression=self.config.network_compression)
                try:
                    plain_rsync.from_file_list(['recovery.conf'],
                                              tempdir, ':%s' % dest)
                except CommandFailedException, e:
                    output.exception(
                        'remote copy of recovery.conf failed: %s', e)
                    output.close_and_exit()

                shutil.rmtree(tempdir)
        else:
            # avoid shipping of just recovered pg_xlog files
            output.info("Generating archive status files")
            if remote_command:
                status_dir = tempfile.mkdtemp(prefix='barman_xlog_status-')
            else:
                status_dir = os.path.join(wal_dest, 'archive_status')
                mkpath(status_dir)
            for wal_info in required_xlog_files:
                with open(os.path.join(status_dir, "%s.done" % wal_info.name),
                          'a') as f:
                    f.write('')
            if remote_command:
                try:
                    rsync('%s/' % status_dir,
                          ':%s' % os.path.join(wal_dest, 'archive_status'))
                except CommandFailedException:
                    output.exception(
                        "unable to populate pg_xlog/archive_status"
                        "directory: %s", e)
                    output.close_and_exit()

                shutil.rmtree(status_dir)

        # Disable dangerous setting in the target data dir
        output.info("Disabling dangerous settings in destination directory.")
        if remote_command:
            tempdir = tempfile.mkdtemp(prefix='barman_recovery-')
            pg_config = os.path.join(tempdir, 'postgresql.conf')
            shutil.copy2(
                os.path.join(backup_info.get_data_directory(),
                             'postgresql.conf'), pg_config)
        else:
            pg_config = os.path.join(dest, 'postgresql.conf')
        if self.pg_config_mangle(pg_config,
                                 {'archive_command': 'false'},
                                 "%s.origin" % pg_config):
            output.info("The archive_command was set to 'false' "
                        "to prevent data losses.")

        # Find dangerous options in the configuration file (locations)
        clashes = self.pg_config_detect_possible_issues(pg_config)

        if remote_command:
            try:
                rsync.from_file_list(
                    ['postgresql.conf', 'postgresql.conf.origin'], tempdir,
                    ':%s' % dest)
            except CommandFailedException, e:
                output.exception(
                    'remote copy of configuration files failed: %s', e)
                output.close_and_exit()
            shutil.rmtree(tempdir)

        # Copy the backup.info file to the destination as ".barman-recover.info"
        if remote_command:
            try:
                rsync(backup_info.filename, ':%s/.barman-recover.info' % dest)
            except CommandFailedException, e:
                output.exception(
                    'copy of recovery metadata file failed: %s', e)
                output.close_and_exit()
        else:
            backup_info.save(os.path.join(dest, '.barman-recover.info'))

        output.info("", log=False)
        output.info("Your PostgreSQL server has been successfully prepared for "
                    "recovery!", log=False)
        output.info("", log=False)
        output.info("Please review network and archive related settings in the "
                    "PostgreSQL", log=False)
        output.info("configuration file before starting the just recovered "
                    "instance.", log=False)
        output.info("", log=False)
        # With a PostgreSQL version older than 8.4, it is the user's
        # responsibility to delete the "barman_xlog" directory as the
        # restore_command option in recovery.conf is not supported
        if backup_info.version < 80400 and (target_time or target_xid or (
                target_tli and
                target_tli != backup_info.timeline) or target_name):
            output.info("After the recovery, please remember to remove the "
                        "\"barman_xlog\" directory", log=False)
            output.info("inside the PostgreSQL data directory.", log=False)
            output.info("", log=False)
        if clashes:
            output.info("WARNING: Before starting up the recovered PostgreSQL "
                        "server,", log=False)
            output.info("please review also the settings of the following "
                        "configuration", log=False)
            output.info("options as they might interfere with your current "
                        "recovery attempt:", log=False)
            output.info("", log=False)

            for name, value in sorted(clashes.items()):
                output.info("    %s = %s", name, value, log=False)

            output.info("", log=False)
        output.info("Recovery completed successful.")

    def cron(self, verbose=True):
        """
        Executes maintenance operations, such as WAL trashing.

        If verbose is set to False, outputs something only if there is
        at least one file

        :param bool verbose: report even if no actions
        """
        found = False
        compressor = self.compression_manager.get_compressor()
        with self.server.xlogdb('a') as fxlogdb:
            if verbose:
                output.info("Processing xlog segments for %s",
                            self.config.name,
                            log=False)
            # Get the first available backup
            first_backup_id = self.get_first_backup(BackupInfo.STATUS_NOT_EMPTY)
            first_backup = self.server.get_backup(first_backup_id)
            for filename in sorted(glob(
                    os.path.join(self.config.incoming_wals_directory, '*'))):
                if not found and not verbose:
                    output.info("Processing xlog segments for %s",
                                self.config.name,
                                log=False)
                found = True

                # Create WAL Info object
                wal_info = WalFileInfo.from_file(filename, compression=None)

                # If there are no available backups ...
                if first_backup is None:
                    # ... delete xlog segments only for exclusive backups
                    if BackupOptions.CONCURRENT_BACKUP not in self.config.backup_options:
                        output.info("\tNo base backup available. Trashing file %s"
                                " from server %s",
                                wal_info.name, self.config.name)
                        os.unlink(filename)
                        continue
                # ... otherwise
                else:
                    # ... delete xlog segments older than the first backup
                    if wal_info.name < first_backup.begin_wal:
                        output.info("\tOlder than first backup. Trashing file %s"
                                " from server %s",
                                wal_info.name, self.config.name)
                        os.unlink(filename)
                        continue

                # Report to the user the WAL file we are archiving
                output.info("\t%s", os.path.basename(filename), log=False)
                _logger.info("Archiving %s/%s",
                             self.config.name,
                             os.path.basename(filename))
                # Archive the WAL file
                self.cron_wal_archival(compressor, wal_info)

                # Updates the information of the WAL archive with
                # the latest segments
                fxlogdb.write(wal_info.to_xlogdb_line())
                # flush and fsync for every line
                fxlogdb.flush()
                os.fsync(fxlogdb.fileno())
        if not found and verbose:
            output.info("\tno file found", log=False)

    def cron_retention_policy(self):
        """
        Retention policy management
        """
        if (self.server.enforce_retention_policies and
                self.config.retention_policy_mode == 'auto'):
            available_backups = self.get_available_backups(
                BackupInfo.STATUS_ALL)
            retention_status = self.config.retention_policy.report()
            for bid in sorted(retention_status.iterkeys()):
                if retention_status[bid] == BackupInfo.OBSOLETE:
                    output.info(
                        "Enforcing retention policy: removing backup %s for "
                        "server %s" % (bid, self.config.name))
                    self.delete_backup(available_backups[bid])

    def delete_basebackup(self, backup):
        """
        Delete the basebackup dir of a given backup.

        :param barman.infofile.BackupInfo backup: the backup to delete
        """
        backup_dir = backup.get_basebackup_directory()
        _logger.debug("Deleting base backup directory: %s" % backup_dir)
        shutil.rmtree(backup_dir)

    def delete_backup_data(self, backup):
        """
        Delete the data contained in a given backup.

        :param barman.infofile.BackupInfo backup: the backup to delete
        """
        if backup.tablespaces:
            if backup.backup_version == 2:
                tbs_dir = backup.get_basebackup_directory()
            else:
                tbs_dir = os.path.join(backup.get_data_directory(), 'pg_tblspc')
            for tablespace in backup.tablespaces:
                rm_dir = os.path.join(tbs_dir, str(tablespace.oid))
                _logger.debug("Deleting tablespace %s directory: %s" %
                              (tablespace.name, rm_dir))
                shutil.rmtree(rm_dir)

        pg_data = backup.get_data_directory()
        if os.path.exists(pg_data):
            _logger.debug("Deleting PGDATA directory: %s" % pg_data)
            shutil.rmtree(pg_data)

    def delete_wal(self, wal_info):
        """
        Delete a WAL segment, with the given WalFileInfo

        :param barman.infofile.WalFileInfo wal_info: the WAL to delete
        """

        try:
            os.unlink(wal_info.fullpath(self.server))
            try:
                os.removedirs(os.path.dirname(wal_info.fullpath(self.server)))
            except OSError:
                # This is not an error condition
                # We always try to remove the the trailing directories,
                # this means that hashdir is not empty.
                pass
        except OSError:
            _logger.warning('Expected WAL file %s not found during delete',
                            wal_info.name, exc_info=1)

    def recover_basebackup_copy(self, backup_info, dest, tablespaces=None,
                                remote_command=None, safe_horizon=None):
        """
        Perform the actual copy of the base backup for recovery purposes

        :param barman.infofile.BackupInfo backup_info: the backup to recover
        :param str dest: the destination directory
        :param dict[str,str]|None tablespaces: a tablespace name -> location map
            (for relocation)
        :param str|None remote_command: default None. The remote command to
            recover the base backup, in case of remote backup.
        :param datetime.datetime|None safe_horizon: anything after this time
            has to be checked with checksums
        """

        # Dictionary for paths to be excluded from rsync
        exclude_and_protect = []

        # Set a ':' prefix to remote destinations
        dest_prefix = ''
        if remote_command:
            dest_prefix = ':'

        # Copy tablespaces applying bwlimit when necessary
        if backup_info.tablespaces:
            tablespaces_bw_limit = self.config.tablespace_bandwidth_limit
            # Copy a tablespace at a time
            for tablespace in backup_info.tablespaces:
                # Apply bandwidth limit if requested
                bwlimit = self.config.bandwidth_limit
                if tablespaces_bw_limit and \
                        tablespace.name in tablespaces_bw_limit:
                    bwlimit = tablespaces_bw_limit[tablespace.name]
                # By default a tablespace goes in the same location where
                # it was on the source server when the backup was taken
                location = tablespace.location
                # If a relocation has been requested for this tablespace
                # use the user provided target directory
                if tablespace and tablespace.name in tablespaces:
                    location = tablespaces[tablespace.name]
                # If the tablespace location is inside the data directory,
                # exclude and protect it from being deleted during
                # the data directory copy
                if location.startswith(dest):
                    exclude_and_protect.append(location[len(dest):])
                # Exclude and protect the tablespace from being deleted during
                # the data directory copy
                exclude_and_protect.append("/pg_tblspc/%s" % tablespace.oid)
                # Copy the tablespace using smart copy
                tb_rsync = RsyncPgData(
                    ssh=remote_command,
                    bwlimit=bwlimit,
                    network_compression=self.config.network_compression,
                    check=True)
                try:
                    tb_rsync.smart_copy(
                        '%s/' % backup_info.get_data_directory(tablespace.oid),
                        dest_prefix + location,
                        safe_horizon)
                except CommandFailedException, e:
                    msg = "data transfer failure on directory '%s'" % location
                    raise DataTransferFailure.from_rsync_error(e, msg)

        # Copy the pgdata directory
        rsync = RsyncPgData(
            ssh=remote_command,
            bwlimit=self.config.bandwidth_limit,
            exclude_and_protect=exclude_and_protect,
            network_compression=self.config.network_compression)
        try:
            rsync.smart_copy(
                '%s/' % backup_info.get_data_directory(),
                dest_prefix + dest,
                safe_horizon)
        except CommandFailedException, e:
            msg = "data transfer failure on directory '%s'" % dest
            raise DataTransferFailure.from_rsync_error(e, msg)

        # TODO: Manage different location for configuration files
        # TODO: that were not within the data directory

    def recover_xlog_copy(self, compressor, xlogs, wal_dest,
                          remote_command=None):
        """
        Restore WAL segments

        :param compressor: the compressor for the file (if any)
        :param xlogs: the xlog dictionary to recover
        :param wal_dest: the destination directory for xlog recover
        :param remote_command: default None. The remote command to recover
               the xlog, in case of remote backup.
        """
        rsync = RsyncPgData(
            ssh=remote_command,
            bwlimit=self.config.bandwidth_limit,
            network_compression=self.config.network_compression)
        if remote_command:
            # If remote recovery tell rsync to copy them remotely
            # add ':' prefix to mark it as remote
            # add '/' suffix to ensure it is a directory
            wal_dest = ':%s/' % wal_dest
        else:
            # we will not use rsync: destdir must exists
            mkpath(wal_dest)
        if compressor and remote_command:
            xlog_spool = tempfile.mkdtemp(prefix='barman_xlog-')
        total_wals = sum(map(len, xlogs.values()))
        partial_count = 0
        for prefix in sorted(xlogs):
            batch_len = len(xlogs[prefix])
            partial_count += batch_len
            source_dir = os.path.join(self.config.wals_directory, prefix)
            _logger.info(
                "Starting copy of %s WAL files %s/%s from %s to %s",
                batch_len,
                partial_count,
                total_wals,
                xlogs[prefix][0],
                xlogs[prefix][-1])
            if compressor:
                if remote_command:
                    for segment in xlogs[prefix]:
                        compressor.decompress(os.path.join(source_dir, segment),
                                              os.path.join(xlog_spool, segment))
                    try:
                        rsync.from_file_list(xlogs[prefix],
                                             xlog_spool, wal_dest)
                    except CommandFailedException, e:
                        msg = "data transfer failure while copying WAL files " \
                              "to directory '%s'" % (wal_dest[1:],)
                        raise DataTransferFailure.from_rsync_error(e, msg)

                    # Cleanup files after the transfer
                    for segment in xlogs[prefix]:
                        file_name = os.path.join(xlog_spool, segment)
                        try:
                            os.unlink(file_name)
                        except OSError as e:
                            output.warning(
                                "Error removing temporary file '%s': %s",
                                file_name, e)
                else:
                    # decompress directly to the right place
                    for segment in xlogs[prefix]:
                        compressor.decompress(os.path.join(source_dir, segment),
                                              os.path.join(wal_dest, segment))
            else:
                try:
                    rsync.from_file_list(
                        xlogs[prefix],
                        "%s/" % os.path.join(
                            self.config.wals_directory, prefix),
                        wal_dest)
                except CommandFailedException, e:
                    msg = "data transfer failure while copying WAL files " \
                          "to directory '%s'" % (wal_dest[1:],)
                    raise DataTransferFailure.from_rsync_error(e, msg)

        _logger.info("Finished copying %s WAL files.", total_wals)

        if compressor and remote_command:
            shutil.rmtree(xlog_spool)

    def cron_wal_archival(self, compressor, wal_info):
        """
        Archive a WAL segment from the incoming directory.
        This function returns a WalFileInfo object.

        :param compressor: the compressor for the file (if any)
        :param wal_info: WalFileInfo of the WAL file is being processed
        """
        destfile = wal_info.fullpath(self.server)
        destdir = os.path.dirname(destfile)
        srcfile = os.path.join(self.config.incoming_wals_directory,
                wal_info.name)

        # Run the pre_archive_script if present.
        script = HookScriptRunner(self, 'archive_script', 'pre')
        script.env_from_wal_info(wal_info, srcfile)
        script.run()

        mkpath(destdir)
        if compressor:
            compressor.compress(srcfile, destfile)
            shutil.copystat(srcfile, destfile)
            os.unlink(srcfile)
        else:
            shutil.move(srcfile, destfile)

        # execute fsync() on the archived WAL containing directory
        fsync_dir(destdir)
        # execute fsync() also on the incoming directory
        fsync_dir(self.config.incoming_wals_directory)
        # execute fsync() on the archived WAL file
        file_fd = os.open(destfile, os.O_RDONLY)
        os.fsync(file_fd)
        os.close(file_fd)

        stat = os.stat(destfile)
        wal_info.size = stat.st_size
        wal_info.compression = compressor and compressor.compression

        # Run the post_archive_script if present.
        script = HookScriptRunner(self, 'archive_script', 'post')
        script.env_from_wal_info(wal_info)
        script.run()

    def check(self):
        """
        This function performs some checks on the server.
        Set error code to 1 if any of the checks fails
        """
        if self.config.compression and not self.compression_manager.check():
            output.result('check', self.config.name,
                          'compression settings', False)
        else:
            status = True
            try:
                self.compression_manager.get_compressor()
            except CompressionIncompatibility, field:
                output.result('check', self.config.name,
                              '%s setting' % field, False)
                status = False
            output.result('check', self.config.name,
                          'compression settings', status)

        # Minimum redundancy checks
        no_backups = len(self.get_available_backups())
        if no_backups < self.config.minimum_redundancy:
            status = False
        else:
            status = True
        output.result('check', self.config.name,
                      'minimum redundancy requirements', status,
                      'have %s backups, expected at least %s' %
                      (no_backups, self.config.minimum_redundancy))

        # Execute additional checks defined by the BackupExecutor
        self.executor.check()

    def status(self):
        """
        This function show the server status
        """
        # get number of backups
        no_backups = len(self.get_available_backups())
        output.result('status', self.config.name,
                      "backups_number",
                      "No. of available backups", no_backups)
        output.result('status', self.config.name,
                      "first_backup",
                      "First available backup",
                      self.get_first_backup())
        output.result('status', self.config.name,
                      "last_backup",
                      "Last available backup",
                      self.get_last_backup())
        # Minimum redundancy check. if number of backups minor than minimum
        # redundancy, fail.
        if no_backups < self.config.minimum_redundancy:
            output.result('status', self.config.name,
                          "minimum_redundancy",
                          "Minimum redundancy requirements",
                          "FAILED (%s/%s)" % (
                              no_backups,
                              self.config.minimum_redundancy))
        else:
            output.result('status', self.config.name,
                          "minimum_redundancy",
                          "Minimum redundancy requirements",
                          "satisfied (%s/%s)" % (
                              no_backups,
                              self.config.minimum_redundancy))

        # Output additional status defined by the BackupExecutor
        self.executor.status()

    def get_remote_status(self):
        """
        Build additional remote status lines defined by the BackupManager.

        :rtype: dict[str, None]
        """
        return self.executor.get_remote_status()

    def pg_config_mangle(self, filename, settings, backup_filename=None):
        """
        This method modifies the postgres configuration file,
        commenting settings passed as argument, and adding the barman ones.

        If backup_filename is True, it writes on a backup copy.

        :param filename: the Postgres configuration file
        :param settings: settings to mangle dictionary
        :param backup_filename: default False. If True, work on a copy
        """
        if backup_filename:
            shutil.copy2(filename, backup_filename)

        with open(filename) as f:
            content = f.readlines()

        r = re.compile('^\s*([^\s=]+)\s*=\s*(.*)$')
        mangled = False
        with open(filename, 'w') as f:
            for line in content:
                rm = r.match(line)
                if rm:
                    key = rm.group(1)
                    if key in settings:
                        f.write("#BARMAN# %s" % line)
                        # TODO is it useful to handle none values?
                        f.write("%s = %s\n" % (key, settings[key]))
                        mangled = True
                        continue
                f.write(line)

        return mangled

    def pg_config_detect_possible_issues(self, filename):
        """
        This method looks for any possible issue with PostgreSQL
        location options such as data_directory, config_file, etc.
        It returns a dictionary with the dangerous options that have been found.

        :param filename: the Postgres configuration file
        """

        clashes = {}

        with open(filename) as f:
            content = f.readlines()

        r = re.compile('^\s*([^\s=]+)\s*=\s*(.*)$')
        for line in content:
            rm = r.match(line)
            if rm:
                key = rm.group(1)
                if key in self.DANGEROUS_OPTIONS:
                    clashes[key] = rm.group(2)

        return clashes

    def rebuild_xlogdb(self):
        """
        Rebuild the whole xlog database guessing it from the archive content.
        """
        from os.path import isdir, join

        output.info("Rebuilding xlogdb for server %s", self.config.name)
        root = self.config.wals_directory
        default_compression = self.config.compression
        wal_count = label_count = history_count = 0
        # lock the xlogdb as we are about replacing it completely
        with self.server.xlogdb('w') as fxlogdb:
            xlogdb_new = fxlogdb.name + ".new"
            with open(xlogdb_new, 'w') as fxlogdb_new:
                for name in sorted(os.listdir(root)):
                    # ignore the xlogdb and its lockfile
                    if name.startswith(self.server.XLOG_DB):
                        continue
                    fullname = join(root, name)
                    if isdir(fullname):
                        # all relevant files are in subdirectories
                        hash_dir = fullname
                        for wal_name in sorted(os.listdir(hash_dir)):
                            fullname = join(hash_dir, wal_name)
                            if isdir(fullname):
                                _logger.warning(
                                    'unexpected directory '
                                    'rebuilding the wal database: %s',
                                    fullname)
                            else:
                                if xlog.is_wal_file(fullname):
                                    wal_count += 1
                                elif xlog.is_backup_file(fullname):
                                    label_count += 1
                                else:
                                    _logger.warning(
                                        'unexpected file '
                                        'rebuilding the wal database: %s',
                                        fullname)
                                    continue
                                wal_info = WalFileInfo.from_file(
                                    fullname,
                                    default_compression=default_compression)
                                fxlogdb_new.write(wal_info.to_xlogdb_line())
                    else:
                        # only history files are here
                        if xlog.is_history_file(fullname):
                            history_count += 1
                            wal_info = WalFileInfo.from_file(
                                fullname,
                                default_compression=default_compression)
                            fxlogdb_new.write(wal_info.to_xlogdb_line())
                        else:
                            _logger.warning(
                                'unexpected file '
                                'rebuilding the wal database: %s',
                                fullname)
                os.fsync(fxlogdb_new.fileno())
            shutil.move(xlogdb_new, fxlogdb.name)
            fsync_dir(os.path.dirname(fxlogdb.name))
        output.info('Done rebuilding xlogdb for server %s '
                    '(history: %s, backup_labels: %s, wal_file: %s)',
                    self.config.name, history_count, label_count, wal_count)

    def remove_wal_before_backup(self, backup_info):
        """
        Remove WAL files which have been archived before the start of
        the provided backup.

        If no backup_info is provided delete all available WAL files

        :param BackupInfo|None backup_info: the backup information structure
        :return list: a list of removed WAL files
        """
        removed = []
        with self.server.xlogdb() as fxlogdb:
            xlogdb_new = fxlogdb.name + ".new"
            with open(xlogdb_new, 'w') as fxlogdb_new:
                for line in fxlogdb:
                    wal_info = WalFileInfo.from_xlogdb_line(line)
                    if backup_info and wal_info.name >= backup_info.begin_wal:
                        fxlogdb_new.write(wal_info.to_xlogdb_line())
                        continue
                    else:
                        # Delete the WAL segment
                        self.delete_wal(wal_info)
                        removed.append(wal_info.name)
                fxlogdb_new.flush()
                os.fsync(fxlogdb_new.fileno())
            shutil.move(xlogdb_new, fxlogdb.name)
            fsync_dir(os.path.dirname(fxlogdb.name))
        return removed

    def validate_last_backup_maximum_age(self, last_backup_maximum_age):
        """
        Evaluate the age of the last available backup in a catalogue.
        If the last backup is older than the specified time interval (age),
        the function returns False. If within the requested age interval,
        the function returns True.

        :param timedate.timedelta last_backup_maximum_age: time interval
            representing the maximum allowed age for the last backup in a server
            catalogue
        :return tuple: a tuple containing the boolean result of the check and
            auxiliary information about the last backup current age
        """
        # Get the ID of the last available backup
        backup_id = self.get_last_backup()
        if backup_id:
            # Get the backup object
            backup = BackupInfo(self.server, backup_id=backup_id)
            now = datetime.datetime.now(dateutil.tz.tzlocal())
            # Evaluate the point of validity
            validity_time = now - last_backup_maximum_age
            # Pretty print of a time interval (age)
            msg = human_readable_timedelta(now - backup.end_time)
            # If the backup end time is older than the point of validity,
            # return False, otherwise return true
            if backup.end_time < validity_time:
                return False, msg
            else:
                return True, msg
        else:
            # If no backup is available return false
            return False, "No available backups"

    def backup_fsync_and_set_sizes(self, backup_info):
        """
        Fsync all files in a backup and set the actual size on disk of a backup.

        Also evaluate the deduplication ratio and the deduplicated size if
        applicable.

        :param barman.infofile.BackupInfo backup_info: the backup to update
        """
        # Calculate the base backup size
        self.executor.current_action = "calculating backup size"
        _logger.debug(self.executor.current_action)
        backup_size = 0
        deduplicated_size = 0
        backup_dest = backup_info.get_data_directory()
        for dir_path, _, file_names in os.walk(backup_dest):
            # execute fsync() on the containing directory
            fsync_dir(dir_path)
            # execute fsync() on all the contained files
            for filename in file_names:
                file_path = os.path.join(dir_path, filename)
                file_fd = os.open(file_path, os.O_RDONLY)
                file_stat = os.fstat(file_fd)
                backup_size += file_stat.st_size
                # Excludes hard links from real backup size
                if file_stat.st_nlink == 1:
                    deduplicated_size += file_stat.st_size
                os.fsync(file_fd)
                os.close(file_fd)
        # Save size into BackupInfo object
        backup_info.set_attribute('size', backup_size)
        backup_info.set_attribute('deduplicated_size', deduplicated_size)
        if backup_info.size > 0:
            deduplication_ratio = 1 - (float(
                backup_info.deduplicated_size) / backup_info.size)
        else:
            deduplication_ratio = 0

        if self.config.reuse_backup == 'link':
            output.info(
                "Backup size: %s. Actual size on disk: %s"
                " (-%s deduplication ratio)." % (
                pretty_size(backup_info.size),
                pretty_size(backup_info.deduplicated_size),
                '{percent:.2%}'.format(percent=deduplication_ratio)
                ))
        else:
            output.info("Backup size: %s" %
                        pretty_size(backup_info.size))
