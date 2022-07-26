# Copyright 2016, 2017, 2018 Nathan Sommer and Ben Coleman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


"""
Provides a class for storing student submission information and running tests
on the submission.
"""

import os
from time import strftime, time
from tempfile import TemporaryDirectory, mkdtemp

from gkeepserver.directory_locks import directory_locks
from gkeepcore.action_scripts import get_action_script_and_interpreter
from gkeepcore.gkeep_exception import GkeepException
from gkeepcore.student import Student
from gkeepserver.assignments import AssignmentDirectory
from gkeepserver.database import db
from gkeepserver.gkeepd_logger import gkeepd_logger as logger
from gkeepcore.git_commands import git_clone, git_add_all, git_commit, \
    git_checkout
from gkeepcore.test_env_yaml import TestEnv, TestEnvType
from gkeepcore.system_commands import cp, sudo_chown, rm, chmod, mv
from gkeepcore.shell_command import run_command
from gkeepserver.email_sender_thread import email_sender
from gkeepserver.info_update_thread import info_updater
from gkeepserver.reports import reports_clone
from gkeepserver.server_configuration import config
from gkeepserver.server_email import Email
from gkeepcore.path_utils import parse_submission_repo_path, user_home_dir


class Submission:
    """
    Stores student submission information and allows test running.
    """

    def __init__(self, student: Student, student_repo_path, commit_hash,
                 assignment_dir: AssignmentDirectory, faculty_username,
                 faculty_email):
        """
        Simply assign the attributes.

        :param student: Student object containing information about the student
        :param student_repo_path: path to the student's assignment repository
        :param commit_hash: the hash of the commit of the submission
        :param faculty_email: email address of the faculty that owns the
         assignment
        """

        self.assignment_dir = assignment_dir
        self.student = student
        self.student_repo_path = student_repo_path
        self.commit_hash = commit_hash
        self.tests_path = assignment_dir.tests_path
        self.reports_repo_path = assignment_dir.reports_repo_path
        self.faculty_username = faculty_username
        self.faculty_email = faculty_email
        self.class_name = assignment_dir.class_name
        self.assignment_name = assignment_dir.assignment_name
        self.test_env_path = assignment_dir.test_env_path

    def run_tests(self):
        """
        Run tests on the student's submission.

        Test results are emailed to the student and placed in the reports
        repository for the assignment.

        This thread must terminate in a reasonable amount of time or it may
        prevent future tests from being run and will prevent gkeepd from
        cleanly shutting down.

        Creates a directory in the tester user's home directory in which to
        run the tests.
        """

        faculty_username, class_name, assignment_name = \
            parse_submission_repo_path(self.student_repo_path)

        if not db.class_is_open(class_name, faculty_username):
            self._send_closed_email(class_name, assignment_name)
            return

        if db.is_disabled(class_name, assignment_name, faculty_username):
            self._send_disabled_email(class_name, assignment_name)
            return

        logger.log_debug('Running tests on {0}'.format(self.student_repo_path))

        temp_path = mkdtemp(dir=user_home_dir(config.tester_user),
                            prefix='{}_'.format(int(time())))
        chmod(temp_path, '770')

        temp_assignment_path = os.path.join(temp_path, assignment_name)

        git_clone(self.student_repo_path, temp_path)
        git_checkout(temp_assignment_path, self.commit_hash)

        # copy the tests - this creates a tests folder inside the temp dir
        with directory_locks.get_lock(self.assignment_dir.path):
            cp(self.tests_path, temp_path, recursive=True)
            # copy the test_env.yaml file (if present)
            if os.path.exists(self.test_env_path):
                cp(self.test_env_path, temp_path)

        temp_run_action_sh_path = os.path.join(temp_path, 'run_action.sh')

        test_env = TestEnv(os.path.join(temp_path, 'test_env.yaml'))
        if test_env.type == TestEnvType.FIREJAIL:
            # firejail makes the temp directory look like /home/tester from
            # the tests point of view
            write_run_action_sh(temp_run_action_sh_path, self.tests_path,
                                user_home_dir(config.tester_user))
        elif test_env.type == TestEnvType.DOCKER:
            write_run_action_sh(temp_run_action_sh_path, self.tests_path)
        else:
            write_run_action_sh(temp_run_action_sh_path, self.tests_path,
                                temp_path)

        temp_assignment_path = os.path.join(temp_path, assignment_name)

        # make the tester user the owner of the temporary directory
        sudo_chown(temp_path, config.tester_user, config.keeper_group,
                   recursive=True)

        # execute action.sh and capture the output
        try:
            test_env = TestEnv(self.test_env_path)
            if test_env.type == TestEnvType.DOCKER:
                # Run a docker pull on the image here so that the output
                # is not captured in the email.  If the container image is
                # already on the server, this will return quickly
                pull_cmd = ['docker', 'pull', test_env.image]
                run_command(pull_cmd)
                cmd = self._make_docker_command(temp_path, assignment_name,
                                                test_env.image)
            elif test_env.type == TestEnvType.FIREJAIL:
                cmd = self._make_firejail_command(temp_path, assignment_name,
                                                  test_env.append_args)
            elif test_env.type == TestEnvType.HOST:
                cmd = self._make_host_command(temp_run_action_sh_path,
                                              temp_assignment_path)
            else:
                raise GkeepException('Unknown test env type {}'
                                     .format(test_env.type))

            body = run_command(cmd)

            # send output as email
            subject = ('[{0}] {1} submission test results'
                       .format(class_name, assignment_name))
            email_sender.enqueue(Email(self.student.email_address, subject,
                                       body, html_pre_body=True))

            if self.student.username != faculty_username:
                with directory_locks.get_lock(self.assignment_dir.path):
                    self._add_report(body)
                info_updater.enqueue_submission_scan(self.faculty_username,
                                                     self.class_name,
                                                     self.assignment_name,
                                                     self.student.username)

        except Exception as e:
            report_failure(assignment_name, self.student,
                           self.faculty_email, str(e))

        if os.path.isdir(temp_path):
            rm(temp_path, recursive=True, sudo=True)

        logger.log_debug('Done running tests on {0}'
                         .format(self.student_repo_path))

    def _add_report(self, body):
        # Add the results of running tests to the reports repository
        with reports_clone(self.assignment_dir) as temp_reports_repo_path:
            last_first_username = self.student.get_last_first_username()

            student_report_dir_path = os.path.join(temp_reports_repo_path,
                                                   last_first_username)
            os.makedirs(student_report_dir_path, exist_ok=True)

            timestamp = strftime('%Y-%m-%d_%H-%M-%S-%Z')
            report_filename = 'report-{0}.txt'.format(timestamp)
            report_file_path = os.path.join(student_report_dir_path,
                                            report_filename)

            counter = 1
            while os.path.exists(report_file_path):
                report_filename = 'report-{0}-{1}.txt'.format(timestamp,
                                                              counter)
                report_file_path = os.path.join(student_report_dir_path,
                                                report_filename)
                counter += 1

            with open(report_file_path, 'w') as f:
                f.write(body)

            reports_commit_message = ('Submission report for {}'
                                      .format(last_first_username))

            git_add_all(temp_reports_repo_path)
            git_commit(temp_reports_repo_path, reports_commit_message)

    def _make_docker_command(self, temp_path, assignment_name,
                             container_image):
        return ['docker', 'run', '-v',
                '{}:/git-keeper-tester'.format(temp_path),
                container_image, 'bash',
                '/git-keeper-tester/run_action.sh',
                os.path.join('/git-keeper-tester/', assignment_name),
                self.student.username, self.student.email_address,
                self.student.last_name, self.student.first_name]

    def _make_host_command(self, temp_run_action_sh_path,
                           temp_assignment_path):
        return ['sudo', '--user', config.tester_user, '--set-home', 'bash',
                temp_run_action_sh_path, temp_assignment_path,
                self.student.username, self.student.email_address,
                self.student.last_name, self.student.first_name]

    def _make_firejail_command(self, temp_path, assignment_name, append_args):
        tester_home = user_home_dir(config.tester_user)

        cmd = ['sudo', '--user', config.tester_user, '--set-home',
               'firejail', '--noprofile', '--quiet',
               '--private={}'.format(temp_path)]

        if append_args is not None:
            cmd.extend(append_args.split())

        cmd.extend(['bash', os.path.join(tester_home, 'run_action.sh'),
                    os.path.join(tester_home, assignment_name),
                    self.student.username, self.student.email_address,
                    self.student.last_name, self.student.first_name])

        return cmd

    def _send_closed_email(self, class_name, assignment_name):
        # Send an email to the submitter that the class has closed
        subject = ('[{0}] class is closed'.format(class_name))
        body = ('You have pushed a submission for a class that is is '
                'closed.')
        email_sender.enqueue(Email(self.student.email_address, subject,
                                   body))
        logger.log_info('{} pushed to {} in {}, which is closed'
                        .format(self.student.username, assignment_name,
                                class_name))

    def _send_disabled_email(self, class_name, assignment_name):
        # Send an email to the submitter that the class is disabled
        subject = ('[{0}] assignment {1} is disabled'
                   .format(class_name, assignment_name))
        body = ('You have pushed a submission for assignment {} which is '
                'disabled. No tests were run on your submission.'
                .format(assignment_name))
        email_sender.enqueue(Email(self.student.email_address, subject,
                                   body))
        logger.log_info('{} pushed to {} in {}, which is disabled'
                        .format(self.student.username, assignment_name,
                                class_name))


def write_run_action_sh(dest_path: str, tests_path: str, run_path=None):
    """
    Write run_action.sh before testing.

    The contents of the script depend on the type of action script used in the
    uploaded assignment.

    :param dest_path: directory in which to place run_action.sh
    :param tests_path: path to a directory containing the tests
    :param run_path: the path containing the tests folder, which will be the
      same as dest_path unless using firejail
    """
    temp_dir = TemporaryDirectory()
    temp_dir_path = temp_dir.name

    temp_run_action_sh_path = os.path.join(temp_dir_path, 'run_action.sh')

    if run_path is None:
        cd_command = ''
    else:
        cd_command = 'cd {}/tests'.format(run_path)

    template = '''#!/bin/bash
{cd_command}
GLOBAL_TIMEOUT={global_timeout}
GLOBAL_MEM_LIMIT_MB={global_memory_limit}
GLOBAL_MEM_LIMIT_KB=$(($GLOBAL_MEM_LIMIT_MB * 1024))
ulimit -v $GLOBAL_MEM_LIMIT_KB
trap 'kill -INT -$pid' INT
timeout $GLOBAL_TIMEOUT {interpreter} {script_name} "$@" &
pid=$!
wait $pid
'''

    script_name, interpreter = get_action_script_and_interpreter(tests_path)

    if script_name is None or interpreter is None:
        raise GkeepException('No valid action script found')

    run_action_sh_contents = \
        template.format(cd_command=cd_command,
                        global_timeout=config.tests_timeout,
                        global_memory_limit=config.tests_memory_limit,
                        interpreter=interpreter,
                        script_name=script_name)

    with open(temp_run_action_sh_path, 'w') as f:
        f.write(run_action_sh_contents)

    mv(temp_run_action_sh_path, dest_path, sudo=True)


def report_failure(assignment, student, faculty_email, message):
    s_subject = ('{0}: Failed to process submission - contact instructor'
                 .format(assignment))
    s_body = ['Your submission was received, but something went wrong.',
              'This is likely your instructor\'s fault, not yours.',
              'Please contact your instructor about this error!']

    email_sender.enqueue(Email(student.email_address, s_subject, s_body))

    f_subject = 'git-keeper run_tests failure'
    f_body = ['student: {0} {1}'.format(student.first_name, student.last_name),
              'email: {0}'.format(student.email_address),
              'assignment {0}'.format(assignment),
              'further information:',
              message]

    email_sender.enqueue(Email(faculty_email, f_subject, f_body))
