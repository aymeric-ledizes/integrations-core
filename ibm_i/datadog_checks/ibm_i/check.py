# (C) Datadog, Inc. 2021-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from contextlib import closing, suppress
from datetime import datetime
from typing import List, NamedTuple, Tuple

import os
import subprocess
import fcntl
import time

from datadog_checks.base import AgentCheck
from datadog_checks.base.utils.db import QueryManager

from . import queries
from .config_models import ConfigMixin

SystemInfo = NamedTuple('SystemInfo', [('hostname', str), ('os_version', int), ('os_release', int)])


class IbmICheck(AgentCheck, ConfigMixin):
    SERVICE_CHECK_NAME = "ibm_i.can_connect"

    def __init__(self, name, init_config, instances):
        super(IbmICheck, self).__init__(name, init_config, instances)

        self._connection_string = None
        self._subprocess = None
        self._subprocess_stderr = None
        self._subprocess_stdout = None
        self._subprocess_stdin = None
        self._query_manager = None
        self.check_initializations.append(self.set_up_query_manager)

    def handle_query_error(self, error):
        self._current_errors += 1
        return error

    def check(self, _):
        check_start = datetime.now()
        self._current_errors = 0

        try:
            self.query_manager.execute()
            check_status = AgentCheck.OK
        except AttributeError:
            self.warning('Could not set up query manager, skipping check run')
            check_status = None
        except Exception as e:
            self._delete_connection_subprocess()
            check_status = AgentCheck.CRITICAL

        if check_status is not None:
            self.service_check(
                self.SERVICE_CHECK_NAME,
                check_status,
                tags=self.config.tags,
                hostname=self._query_manager.hostname,
            )

        check_end = datetime.now()
        check_duration = check_end - check_start
        self.log.debug("Check duration: %s", check_duration)

        if check_status is not None:
            # The list() conversion is needed as self.config.tags is a tuple
            check_duration_tags = list(self.config.tags) + ["check_id:{}".format(self.check_id)]
            self.gauge(
                "ibm_i.check.duration",
                check_duration.total_seconds(),
                check_duration_tags,
                hostname=self._query_manager.hostname,
            )

    def _create_connection_subprocess(self):
        (r1, w1) = os.pipe()  # for process -> subprocess stdin writes
        (r2, w2) = os.pipe()  # for subprocess -> process stdout writes
        (r3, w3) = os.pipe()  # for subprocess -> process stderr writes

        self._subprocess = subprocess.Popen(
                ["/opt/datadog-agent/embedded/bin/python3",
                 "-c", "from datadog_checks.ibm_i.query_script import query; query()"],
                stdin=r1,
                stdout=w2,
                stderr=w3,
                text=True,
            )

        self._subprocess_stdin = os.fdopen(w1, 'w', buffering=1)
        self._subprocess_stdout = os.fdopen(r2)

        # Set stdout reader as non-blocking, we don't want to
        # block .read() calls to be able to time out.
        fl = fcntl.fcntl(self._subprocess_stdout.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(self._subprocess_stdout, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        self._subprocess_stderr = os.fdopen(r3)

        # Set stderr reader as non-blocking, we don't want to
        # wait until EOF is sent, we only want to read whatever is there when
        # we try to return errors.
        fl = fcntl.fcntl(self._subprocess_stderr.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(self._subprocess_stderr, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        self._subprocess_stdin.write("{}{}".format(self.connection_string, os.linesep))

    def _delete_connection_subprocess(self):
        if self._subprocess:
            self._subprocess.kill()
        self._subprocess = None
        
        if self._subprocess_stdin:
            self._subprocess_stdin.close()
        self._subprocess_stdin = None
        
        if self._subprocess_stdout:
            self._subprocess_stdout.close()
        self._subprocess_stdout = None

        if self._subprocess_stderr:
            self._subprocess_stderr.close()
        self._subprocess_stderr = None

    def execute_query(self, query):
        if not self._subprocess:
            self._create_connection_subprocess()

        # Write query
        self._subprocess_stdin.write("{}{}".format(query, os.linesep))

        done = False
        query_start = datetime.now()

        while not done and (datetime.now() - query_start).total_seconds() <= self.config.query_timeout:
            time.sleep(0.1)
            try:
                lines = self._subprocess_stdout.read().strip().split(os.linesep)
                for line in lines:
                    stripped_line = line.strip()
                    if stripped_line == "ENDOFQUERY":
                        done = True
                        break
                    yield [el for el in stripped_line.split('|')]
            except TypeError:
                # We couldn't read anything
                continue

        e = None
        try:
            e = self._subprocess_stderr.read().strip()
        except TypeError:
            # We couldn't read anything
            pass

        if e:
            self._delete_connection_subprocess()
            raise Exception(e)

        if not done:
            self._delete_connection_subprocess()
            raise Exception("Timed out")

    @property
    def connection_string(self):
        if self._connection_string is None:
            # https://www.connectionstrings.com/as-400/
            # https://www.ibm.com/support/pages/odbc-driver-ibm-i-access-client-solutions
            connection_string = self.config.connection_string
            if not connection_string:
                connection_string = f'Driver={{{self.config.driver.strip("{}")}}};'

                if self.config.system:
                    connection_string += f'System={self.config.system};'

                if self.config.username:
                    connection_string += f'UID={self.config.username};'

                if self.config.password:
                    connection_string += f'PWD={self.config.password};'
                    self.register_secret(self.config.password)

            self._connection_string = connection_string

        return self._connection_string

    @property
    def query_manager(self):
        if self._query_manager is None:
            self.set_up_query_manager()
        return self._query_manager

    def set_up_query_manager(self):
        system_info = self.fetch_system_info()
        if system_info:
            query_list = [
                queries.BaseDiskUsage,
                queries.CPUUsage,
                queries.InactiveJobStatus,
                queries.ActiveJobStatus,
                queries.JobMemoryUsage,
                queries.MemoryInfo,
                queries.JobQueueInfo,
                queries.get_message_queue_info(self.config.severity_threshold),
            ]
            if system_info.os_version > 7 or (system_info.os_version == 7 and system_info.os_release >= 3):
                query_list.append(queries.DiskUsage)
                query_list.append(queries.SubsystemInfo)

            if self.config.fetch_ibm_mq_metrics and self.ibm_mq_check():
                query_list.append(queries.IBMMQInfo)

            self._query_manager = QueryManager(
                self,
                self.execute_query,
                tags=self.config.tags,
                queries=query_list,
                hostname=system_info.hostname,
            )
            self._query_manager.compile_queries()

    def ibm_mq_check(self):
        # Try to get data from the IBM MQ tables. If they're not present,
        # an exception is raised, and we return that IBM MQ is not available.
        query = "SELECT QNAME, COUNT(*) FROM TABLE(MQREADALL()) GROUP BY QNAME"
        try:
            # self.execute_query(query) yields a generator, therefore the SQL query is actually run
            # only when needed (eg. when looping through it, transforming it
            # into a list, using next on it to get the next element).
            # We do need the query to be executed, which is why we do an operation on it.
            # We use the list operation because we know it will work as long as the query doesn't
            # raise an error (if we were to use next, we'd have to take care of the case where
            # the generator rasies a StopIteration exception because the query is valid but returns 0 rows).
            list(self.execute_query(query))  # type: List[Tuple[str]]
        except Exception as e:
            self.log.debug("Couldn't find IBM MQ data, turning off IBM MQ queries: %s", e)
            return False

        return True

    def fetch_system_info(self):
        try:
            return self.system_info_query()
        except Exception as e:
            self._delete_connection_subprocess()

    def system_info_query(self):
        query = "SELECT HOST_NAME, OS_VERSION, OS_RELEASE FROM SYSIBMADM.ENV_SYS_INFO"
        results = list(self.execute_query(query))  # type: List[Tuple[str]]
        if len(results) == 0:
            self.log.error("Couldn't find system info on the remote system.")
            return None
        if len(results) > 1:
            self.log.error("Too many results returned by system query. Expected 1, got %d", len(results))
            return None

        info_row = results[0]
        if len(info_row) != 3:
            self.log.error("Expected 3 columns in system info query, got %d", len(info_row))
            return None

        hostname = info_row[0]
        try:
            os_version = int(info_row[1])
        except ValueError:
            self.log.error("Expected integer for OS version, got %s", info_row[1])
            return None

        try:
            os_release = int(info_row[2])
        except ValueError:
            self.log.error("Expected integer for OS release, got %s", info_row[2])
            return None

        return SystemInfo(hostname=hostname, os_version=os_version, os_release=os_release)