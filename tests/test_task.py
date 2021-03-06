"""Tests for taskgraph."""
import hashlib
import importlib
import logging
import logging.handlers
import multiprocessing
import os
import pickle
import re
import shutil
import sqlite3
import tempfile
import time
import unittest

import mock
import taskgraph

LOGGER = logging.getLogger(__name__)

N_TEARDOWN_RETRIES = 5


def _noop_function(**kwargs):
    """Does nothing except allow kwargs to be passed."""
    pass


def _long_running_function(delay):
    """Wait for `delay` seconds."""
    time.sleep(delay)


def _create_two_files_on_disk(value, target_a_path, target_b_path):
    """Create two files and write `value` and append if possible."""
    with open(target_a_path, 'a') as a_file:
        a_file.write(value)

    with open(target_b_path, 'a') as b_file:
        b_file.write(value)


def _merge_and_append_files(base_a_path, base_b_path, target_path):
    """Merge two files and append if possible to new file."""
    with open(target_path, 'a') as target_file:
        for base_path in [base_a_path, base_b_path]:
            with open(base_path, 'r') as base_file:
                target_file.write(base_file.read())


def _create_list_on_disk(value, length, target_path=None):
    """Create a numpy array on disk filled with value of `size`."""
    target_list = [value] * length
    pickle.dump(target_list, open(target_path, 'wb'))


def _call_it(target, *args):
    """Invoke `target` with `args`."""
    target(*args)


def _append_val(path, *val):
    """Append a `val` to file at `path`."""
    with open(path, 'a') as target_file:
        for v in val:
            target_file.write(str(v))


def _sum_lists_from_disk(list_a_path, list_b_path, target_path):
    """Read two lists, add them and save result."""
    list_a = pickle.load(open(list_a_path, 'rb'))
    list_b = pickle.load(open(list_b_path, 'rb'))
    target_list = []
    for a, b in zip(list_a, list_b):
        target_list.append(a+b)
    pickle.dump(target_list, open(target_path, 'wb'))


def _div_by_zero():
    """Divide by zero to raise an exception."""
    return 1/0


def _create_file(target_path, content):
    """Create a file with contents."""
    with open(target_path, 'w') as target_file:
        target_file.write(content)


def _create_file_once(target_path, content):
    """Create a file on the first call, raise an exception on the second."""
    if hasattr(_create_file_once, 'executed'):
        raise RuntimeError("this function was called twice")
    _create_file_once.executed = True
    with open(target_path, 'w') as target_file:
        target_file.write(content)


def _copy_file_once(base_path, target_path):
    """Copy base to target on the first call, raise exception on second."""
    if hasattr(_copy_file_once, 'executed'):
        raise RuntimeError("this function was called twice")
    _copy_file_once.executed = True
    shutil.copyfile(base_path, target_path)


def _copy_two_files_once(base_path, target_a_path, target_b_path):
    """Copy base to target a/b on first call, raise exception on second."""
    if hasattr(_copy_two_files_once, 'executed'):
        raise RuntimeError("this function was called twice")
    _copy_two_files_once.executed = True
    shutil.copyfile(base_path, target_a_path)
    shutil.copyfile(base_path, target_b_path)


def _log_from_another_process(logger_name, log_message):
    """Write a log message to a given logger.

    Parameters:
        logger_name (string): The string logger name to which ``log_message``
            will be logged.
        log_message (string): The string log message to be logged (at INFO
            level) to the logger at ``logger_name``.

    Returns:
        ``None``

    """
    logger = logging.getLogger(logger_name)
    logger.info(log_message)


class TaskGraphTests(unittest.TestCase):
    """Tests for the taskgraph."""

    def setUp(self):
        """Create temp workspace directory."""
        # this lets us delete the workspace after its done no matter the
        # the rest result
        self.workspace_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Remove temporary directory."""
        attempts = 0
        while attempts < N_TEARDOWN_RETRIES:
            try:
                shutil.rmtree(self.workspace_dir)
                break
            except Exception:
                LOGGER.exception('error when tearing down.')
                attempts += 1

    def test_version_loaded(self):
        """TaskGraph: verify we can load the version."""
        try:
            import taskgraph
            # Verifies that there's a version attribute and it has a value.
            self.assertTrue(len(taskgraph.__version__) > 0)
        except Exception:
            self.fail('Could not load the taskgraph version as expected.')

    def test_version_not_loaded(self):
        """TaskGraph: verify exception when not installed."""
        from pkg_resources import DistributionNotFound
        import taskgraph

        with mock.patch('taskgraph.pkg_resources.get_distribution',
                        side_effect=DistributionNotFound('taskgraph')):
            with self.assertRaises(RuntimeError):
                # RuntimeError is a side effect of `import taskgraph`, so we
                # reload it to retrigger the metadata load.
                taskgraph = importlib.reload(taskgraph)

    def test_single_task(self):
        """TaskGraph: Test a single task."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0, 0.1)
        # forcing this one to be unicode since there shouldn't be a problem
        # with that at all...
        target_path = u'%s' % os.path.join(self.workspace_dir, '1000.dat')
        value = 5
        list_len = 1000
        _ = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value, list_len),
            kwargs={
                'target_path': target_path,
            },
            target_path_list=[target_path])
        task_graph.close()
        task_graph.join()
        result = pickle.load(open(target_path, 'rb'))
        self.assertEqual(result, [value]*list_len)

    def test_task_hash_source_deleted(self):
        """TaskGraph: test if old target deleted when hashing duplicate."""
        target_a_path = os.path.join(self.workspace_dir, 'a.txt')
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        task_a = task_graph.add_task(
            func=_create_file,
            args=(target_a_path, 'test value'),
            target_path_list=[target_a_path],
            hash_algorithm='md5',
            copy_duplicate_artifact=True)
        task_a.join()
        target_b_path = os.path.join(self.workspace_dir, 'b.txt')
        _ = task_graph.add_task(
            func=_create_file,
            args=(target_b_path, 'test value'),
            target_path_list=[target_b_path],
            hash_algorithm='md5',
            copy_duplicate_artifact=True)
        task_graph.close()
        task_graph.join()
        del task_graph

        os.remove(target_a_path)
        os.remove(target_b_path)

        target_c_path = os.path.join(self.workspace_dir, 'c.txt')
        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1)
        _ = task_graph.add_task(
            func=_create_file,
            args=(target_c_path, 'test value'),
            target_path_list=[target_c_path],
            hash_algorithm='md5',
            copy_duplicate_artifact=True)
        task_graph.close()
        task_graph.join()

        with open(target_c_path, 'r') as target_file:
            result = target_file.read()
        self.assertEqual(result, 'test value')

    def test_task_rel_vs_absolute(self):
        """TaskGraph: test that relative path equates to absolute."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)

        target_a_path = os.path.relpath(
            os.path.join(self.workspace_dir, 'a.txt'))
        target_b_path = os.path.abspath(target_a_path)

        _ = task_graph.add_task(
           func=_create_file,
           args=(target_a_path, 'test value'),
           target_path_list=[target_a_path],
           hash_algorithm='md5',
           copy_duplicate_artifact=True,
           task_name='task a')

        _ = task_graph.add_task(
           func=_create_file,
           args=(target_b_path, 'test value'),
           target_path_list=[target_b_path],
           hash_algorithm='md5',
           copy_duplicate_artifact=True,
           task_name='task b')

        task_graph.close()
        task_graph.join()
        del task_graph

        with open(target_a_path, 'r') as a_file:
            m = hashlib.md5()
            m.update(a_file.read().encode('utf-8'))
            a_digest = m.digest()
        with open(target_b_path, 'r') as b_file:
            m = hashlib.md5()
            m.update(b_file.read().encode('utf-8'))
            b_digest = m.digest()
        self.assertEqual(a_digest, b_digest)

    def test_timeout_task(self):
        """TaskGraph: Test timeout functionality."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        _ = task_graph.add_task(
            func=_long_running_function,
            args=(5,))
        task_graph.close()
        timedout = not task_graph.join(0.5)
        # this should timeout since function runs for 5 seconds
        self.assertTrue(timedout)

    def test_precomputed_task(self):
        """TaskGraph: Test that a task reuses old results."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        target_path = os.path.join(self.workspace_dir, '1000.dat')
        value = 5
        list_len = 1000
        _ = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value, list_len),
            kwargs={
                'target_path': target_path,
            },
            target_path_list=[target_path])
        task_graph.close()
        task_graph.join()
        result = pickle.load(open(target_path, 'rb'))
        self.assertEqual(result, [value]*list_len)
        result_m_time = os.path.getmtime(target_path)
        del task_graph

        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        _ = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value, list_len),
            kwargs={
                'target_path': target_path,
            },
            target_path_list=[target_path])
        task_graph.close()
        task_graph.join()
        del task_graph

        # taskgraph shouldn't have recomputed the result
        second_result_m_time = os.path.getmtime(target_path)
        self.assertEqual(result_m_time, second_result_m_time)

    def test_task_chain(self):
        """TaskGraph: Test a task chain."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        target_a_path = os.path.join(self.workspace_dir, 'a.dat')
        target_b_path = os.path.join(self.workspace_dir, 'b.dat')
        result_path = os.path.join(self.workspace_dir, 'result.dat')
        result_2_path = os.path.join(self.workspace_dir, 'result2.dat')
        value_a = 5
        value_b = 10
        list_len = 10
        task_a = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value_a, list_len),
            kwargs={
                'target_path': target_a_path,
            },
            target_path_list=[target_a_path])
        task_b = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value_b, list_len),
            kwargs={
                'target_path': target_b_path,
            },
            target_path_list=[target_b_path])
        sum_task = task_graph.add_task(
            func=_sum_lists_from_disk,
            args=(target_a_path, target_b_path),
            kwargs={
                'target_path': result_path,
            },
            target_path_list=[result_path],
            dependent_task_list=[task_a, task_b])
        sum_task.join()

        result = pickle.load(open(result_path, 'rb'))
        self.assertEqual(result, [value_a+value_b]*list_len)

        sum_2_task = task_graph.add_task(
            func=_sum_lists_from_disk,
            args=(target_a_path, result_path, result_2_path),
            target_path_list=[result_2_path],
            dependent_task_list=[task_a, sum_task])
        sum_2_task.join()
        result2 = pickle.load(open(result_2_path, 'rb'))
        expected_result = [(value_a*2+value_b)]*list_len
        self.assertEqual(result2, expected_result)

        sum_3_task = task_graph.add_task(
            func=_sum_lists_from_disk,
            args=(target_a_path, result_path, result_2_path),
            target_path_list=[result_2_path],
            dependent_task_list=[task_a, sum_task])
        task_graph.close()
        sum_3_task.join()
        result3 = pickle.load(open(result_2_path, 'rb'))
        expected_result = [(value_a*2+value_b)]*list_len
        self.assertEqual(result3, expected_result)
        task_graph.join()

    def test_task_chain_single_thread(self):
        """TaskGraph: Test a single threaded task chain."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1)
        target_a_path = os.path.join(self.workspace_dir, 'a.dat')
        target_b_path = os.path.join(self.workspace_dir, 'b.dat')
        result_path = os.path.join(self.workspace_dir, 'result.dat')
        result_2_path = os.path.join(self.workspace_dir, 'result2.dat')
        value_a = 5
        value_b = 10
        list_len = 10
        task_a = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value_a, list_len),
            kwargs={
                'target_path': target_a_path,
            },
            target_path_list=[target_a_path],
            task_name='task a')
        task_b = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value_b, list_len),
            kwargs={
                'target_path': target_b_path,
            },
            target_path_list=[target_b_path],
            task_name='task b')
        sum_task = task_graph.add_task(
            func=_sum_lists_from_disk,
            args=(target_a_path, target_b_path),
            kwargs={
                'target_path': result_path,
            },
            target_path_list=[result_path],
            dependent_task_list=[task_a, task_b],
            task_name='task c')
        sum_task.join()

        result = pickle.load(open(result_path, 'rb'))
        self.assertEqual(result, [value_a+value_b]*list_len)

        sum_2_task = task_graph.add_task(
            func=_sum_lists_from_disk,
            args=(target_a_path, result_path, result_2_path),
            target_path_list=[result_2_path],
            dependent_task_list=[task_a, sum_task],
            task_name='task sum_2')
        sum_2_task.join()
        result2 = pickle.load(open(result_2_path, 'rb'))
        expected_result = [(value_a*2+value_b)]*list_len
        self.assertEqual(result2, expected_result)

        sum_3_task = task_graph.add_task(
            func=_sum_lists_from_disk,
            args=(target_a_path, result_path, result_2_path),
            target_path_list=[result_2_path],
            dependent_task_list=[task_a, sum_task],
            task_name='task sum_3')
        task_graph.close()
        sum_3_task.join()
        result3 = pickle.load(open(result_2_path, 'rb'))
        expected_result = [(value_a*2+value_b)]*list_len
        task_graph.join()
        task_graph = None
        self.assertEqual(result3, expected_result)

        # we should have 4 completed values in the database, 5 total but one
        # was a duplicate
        database_path = os.path.join(
            self.workspace_dir, taskgraph._TASKGRAPH_DATABASE_FILENAME)
        with sqlite3.connect(database_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM taskgraph_data")
            result = cursor.fetchall()
        self.assertEqual(len(result), 4)

    def test_task_broken_chain(self):
        """TaskGraph: Test a multiprocess chain with exception raised."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 4)
        target_a_path = os.path.join(self.workspace_dir, 'a.dat')
        target_b_path = os.path.join(self.workspace_dir, 'b.dat')
        result_path = os.path.join(self.workspace_dir, 'result.dat')
        value_a = 5
        list_len = 10
        task_a = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value_a, list_len),
            kwargs={
                'target_path': target_a_path,
            },
            target_path_list=[target_a_path])
        task_b = task_graph.add_task(
            func=_div_by_zero,
            dependent_task_list=[task_a])
        _ = task_graph.add_task(
            func=_sum_lists_from_disk,
            args=(target_a_path, target_b_path),
            kwargs={
                'target_path': result_path,
            },
            target_path_list=[result_path],
            dependent_task_list=[task_a, task_b])
        task_graph.close()

        with self.assertRaises(ZeroDivisionError):
            task_graph.join()

    def test_broken_task(self):
        """TaskGraph: Test that a task with an exception won't hang."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 1)
        _ = task_graph.add_task(
            func=_div_by_zero, task_name='test_broken_task')
        task_graph.close()
        with self.assertRaises(ZeroDivisionError):
            task_graph.join()

    def test_n_retries(self):
        """TaskGraph: Test a task will attempt to retry after exception."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        result_file_path = os.path.join(self.workspace_dir, 'result.txt')

        fail_task = task_graph.add_task(
            func=Fail(5, result_file_path),
            task_name='fail 5 times', n_retries=5)
        fail_task.join()
        task_graph.close()
        self.assertTrue(os.path.exists(result_file_path))

    def test_broken_task_chain(self):
        """TaskGraph: test dependent tasks fail on ancestor fail."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 4)

        target_path = os.path.join(self.workspace_dir, '1000.dat')
        value = 5
        list_len = 1000
        for task_id in range(1):
            target_path = os.path.join(
                self.workspace_dir, '1000_%d.dat' % task_id)
            normal_task = task_graph.add_task(
                func=_create_list_on_disk,
                args=(value, list_len),
                kwargs={'target_path': target_path},
                target_path_list=[target_path],
                task_name='create list on disk %d' % task_id)
            zero_div_task = task_graph.add_task(
                func=_div_by_zero,
                dependent_task_list=[normal_task],
                task_name='test_broken_task_chain_%d' % task_id)
            target_path = os.path.join(
                self.workspace_dir, 'after_zerodiv_1000_%d.dat' % task_id)
            _ = task_graph.add_task(
                func=_create_list_on_disk,
                args=(value, list_len),
                kwargs={'target_path': target_path},
                dependent_task_list=[zero_div_task],
                target_path_list=[target_path],
                task_name='create list on disk after zero div%d' % task_id)

        task_graph.close()
        with self.assertRaises(ZeroDivisionError):
            task_graph.join()

    def test_empty_task(self):
        """TaskGraph: Test an empty task."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        _ = task_graph.add_task()
        task_graph.close()
        task_graph.join()
        # we shouldn't have anything in the database
        database_path = os.path.join(
            self.workspace_dir, taskgraph._TASKGRAPH_DATABASE_FILENAME)
        with sqlite3.connect(database_path) as conn:
            cursor = conn.cursor()
            cursor.executescript("SELECT * FROM taskgraph_data")
            result = cursor.fetchall()
        self.assertEqual(len(result), 0)

    def test_closed_graph(self):
        """TaskGraph: Test adding to an closed task graph fails."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        task_graph.close()
        target_path = os.path.join(self.workspace_dir, '1000.dat')
        value = 5
        list_len = 1000
        with self.assertRaises(ValueError):
            _ = task_graph.add_task(
                func=_create_list_on_disk,
                args=(value, list_len),
                kwargs={'target_path': target_path},
                target_path_list=[target_path])
        task_graph.join()

    def test_single_task_multiprocessing(self):
        """TaskGraph: Test a single task with multiprocessing."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 1)
        target_path = os.path.join(self.workspace_dir, '1000.dat')
        value = 5
        list_len = 1000
        _ = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value, list_len),
            kwargs={
                'target_path': target_path,
            },
            target_path_list=[target_path])
        task_graph.close()
        task_graph.join()
        result = pickle.load(open(target_path, 'rb'))
        self.assertEqual(result, [value]*list_len)

    def test_get_file_stats(self):
        """TaskGraph: Test _get_file_stats subroutine."""
        from taskgraph.Task import _get_file_stats
        test_dir = os.path.join(self.workspace_dir, 'test_dir')
        test_file = os.path.join(test_dir, 'test_file.txt')
        os.mkdir(test_dir)
        with open(test_file, 'w') as f:
            f.write('\n')
        nofile = os.path.join(self.workspace_dir, 'nofile')
        base_value = [
            nofile, test_dir, test_file,
            10, {'a': {'b': test_file}}, {'a': {'b': test_dir, 'foo': 9}}]
        ignore_dir_result = list(_get_file_stats(
            base_value, 'sizetimestamp', [], True))
        # should get two results if we ignore the directories because there's
        # only two files
        self.assertEqual(len(ignore_dir_result), 2)
        dir_result = list(_get_file_stats(
            base_value, 'sizetimestamp', [], False))
        # should get four results if we track directories because of two files
        # and two directories
        self.assertEqual(len(dir_result), 4)

        result = list(_get_file_stats(nofile, 'sizetimestamp', [], False))
        self.assertEqual(result, [])

    def test_encapsulatedtaskop(self):
        """TaskGraph: Test abstract closure task class."""
        from taskgraph.Task import EncapsulatedTaskOp

        class TestAbstract(EncapsulatedTaskOp):
            def __init__(self):
                pass

        # __call__ is abstract so TypeError since it's not implemented
        with self.assertRaises(TypeError):
            _ = TestAbstract()

        class TestA(EncapsulatedTaskOp):
            def __call__(self, x):
                return x

        class TestB(EncapsulatedTaskOp):
            def __call__(self, x):
                return x

        # TestA and TestB should be different because of different class names
        a = TestA()
        b = TestB()
        # results of calls should be the same
        self.assertEqual(a.__call__(5), b.__call__(5))
        self.assertNotEqual(a.__name__, b.__name__)

        # two instances with same args should be the same
        self.assertEqual(TestA().__name__, TestA().__name__)

        # redefine TestA so we get a different hashed __name__
        class TestA(EncapsulatedTaskOp):
            def __call__(self, x):
                return x*x

        new_a = TestA()
        self.assertNotEqual(a.__name__, new_a.__name__)

        # change internal class constructor to get different hashes
        class TestA(EncapsulatedTaskOp):
            def __init__(self, q):
                super(TestA, self).__init__(q)
                self.q = q

            def __call__(self, x):
                return x*x

        init_new_a = TestA(1)
        self.assertNotEqual(new_a.__name__, init_new_a.__name__)

    def test_repeat_targetless_runs(self):
        """TaskGraph: ensure that repeated runs with no targets reexecute."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1)
        target_path = os.path.join(self.workspace_dir, '1000.dat')
        value = 5
        list_len = 1000
        _ = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value, list_len),
            kwargs={
                'target_path': target_path,
            })
        task_graph.close()
        task_graph.join()
        task_graph = None

        os.remove(target_path)

        task_graph2 = taskgraph.TaskGraph(self.workspace_dir, -1)
        _ = task_graph2.add_task(
            func=_create_list_on_disk,
            args=(value, list_len),
            kwargs={
                'target_path': target_path,
            })

        task_graph2.close()
        task_graph2.join()

        self.assertTrue(
            os.path.exists(target_path),
            "Expected file to exist because taskgraph should have re-run.")

    def test_repeat_targeted_runs(self):
        """TaskGraph: ensure that repeated runs with targets can join."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1)
        target_path = os.path.join(self.workspace_dir, '1000.dat')
        value = 5
        list_len = 1000
        _ = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value, list_len),
            kwargs={
                'target_path': target_path,
            },
            target_path_list=[target_path])
        task_graph.close()
        task_graph.join()
        task_graph = None

        task_graph2 = taskgraph.TaskGraph(self.workspace_dir, -1)
        task = task_graph2.add_task(
            func=_create_list_on_disk,
            args=(value, list_len),
            kwargs={
                'target_path': target_path,
            },
            target_path_list=[target_path])
        self.assertTrue(task.join(1.0), "join failed after 1 second")
        task_graph2.close()
        task_graph2.join()

    def test_task_equality(self):
        """TaskGraph: test correctness of == and != for Tasks."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1)
        target_path = os.path.join(self.workspace_dir, '1000.dat')
        value = 5
        list_len = 1000
        task_a = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value, list_len),
            kwargs={'target_path': target_path},
            target_path_list=[target_path])
        task_a_same = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value, list_len),
            kwargs={'target_path': target_path},
            target_path_list=[target_path])
        task_b = task_graph.add_task(
            func=_create_list_on_disk,
            args=(value+1, list_len),
            kwargs={'target_path': target_path},
            target_path_list=[target_path])

        self.assertTrue(task_a == task_a)
        self.assertTrue(task_a == task_a_same)
        self.assertTrue(task_a != task_b)

    def test_async_logging(self):
        """TaskGraph: ensure async logging can execute."""
        task_graph = taskgraph.TaskGraph(
            self.workspace_dir, 0, reporting_interval=0.5)
        _ = task_graph.add_task(
            func=_long_running_function,
            args=(1.0,))
        task_graph.close()
        task_graph.join()
        timedout = not task_graph.join(5)
        # this should not timeout since function runs for 1 second
        self.assertFalse(timedout, "task timed out")

    def test_scrub(self):
        """TaskGraph: ensure scrub is not scrubbing base types."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)

        target_path = os.path.join(self.workspace_dir, 'a.txt')
        first_task = task_graph.add_task(
            func=_append_val,
            args=(target_path, 1, [1], {'x': 1}),
            task_name='first append')

        second_task = task_graph.add_task(
            func=_append_val,
            args=(target_path, 1, [1], {'x': 2}),
            dependent_task_list=[first_task],
            task_name='second append')

        _ = task_graph.add_task(
            func=_append_val,
            args=(target_path, 1, [2], {'x': 1}),
            dependent_task_list=[second_task],
            task_name='third append')

        task_graph.close()
        task_graph.join()

        with open(target_path, 'r') as target_file:
            file_value = target_file.read()
        self.assertEqual("1[1]{'x': 1}1[1]{'x': 2}1[2]{'x': 1}", file_value)

    def test_target_path_order(self):
        """TaskGraph: ensure target path order doesn't matter."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        target_a_path = os.path.join(self.workspace_dir, 'a.txt')
        target_b_path = os.path.join(self.workspace_dir, 'b.txt')

        task_graph.add_task(
            func=_create_two_files_on_disk,
            args=("word", target_a_path, target_b_path),
            target_path_list=[target_a_path, target_b_path])

        task_graph.add_task(
            func=_create_two_files_on_disk,
            args=("word", target_a_path, target_b_path),
            target_path_list=[target_b_path, target_a_path])

        task_graph.close()
        task_graph.join()

        with open(target_a_path, 'r') as a_file:
            a_value = a_file.read()

        with open(target_b_path, 'r') as b_file:
            b_value = b_file.read()

        self.assertEqual(a_value, "word")
        self.assertEqual(b_value, "word")

    def test_task_hash_when_ready(self):
        """TaskGraph: ensure tasks don't record execution info until ready."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        target_a_path = os.path.join(self.workspace_dir, 'a.txt')
        target_b_path = os.path.join(self.workspace_dir, 'b.txt')

        create_files_task = task_graph.add_task(
            func=_create_two_files_on_disk,
            args=("word", target_a_path, target_b_path),
            target_path_list=[target_a_path, target_b_path])

        target_merged_path = os.path.join(self.workspace_dir, 'merged.txt')
        task_graph.add_task(
            func=_merge_and_append_files,
            args=(target_a_path, target_b_path, target_merged_path),
            target_path_list=[target_merged_path],
            dependent_task_list=[create_files_task])

        task_graph.join()

        # this second task shouldn't execute because it's a copy of the first
        task_graph.add_task(
            func=_merge_and_append_files,
            args=(target_a_path, target_b_path, target_merged_path),
            target_path_list=[target_merged_path],
            dependent_task_list=[create_files_task])

        task_graph.close()
        task_graph.join()

        with open(target_merged_path, 'r') as target_file:
            target_string = target_file.read()

        self.assertEqual(target_string, "wordword")

    def test_multiprocessed_logging(self):
        """TaskGraph: ensure tasks can log from multiple processes."""
        logger_name = 'foo.hello.world'
        log_message = 'This is coming from another process'
        logger = logging.getLogger(logger_name)
        file_log_path = os.path.join(
            self.workspace_dir, 'test_multiprocessed_logging.log')
        handler = logging.FileHandler(file_log_path)
        handler.setFormatter(
            logging.Formatter(fmt=':%(processName)s:%(message)s:'))
        logger.addHandler(handler)

        task_graph = taskgraph.TaskGraph(self.workspace_dir, 1)
        _ = task_graph.add_task(
            func=_log_from_another_process,
            args=(logger_name, log_message))
        task_graph.close()
        task_graph.join()
        handler.flush()
        del handler
        task_graph._terminate()
        del task_graph

        with open(file_log_path) as log_file:
            message = log_file.read().rstrip()
        process_name, logged_message = re.match(
            ':([^:]*):([^:]*):', message).groups()
        self.assertEqual(logged_message, log_message)
        self.assertNotEqual(
            process_name, multiprocessing.current_process().name)

    def test_repeated_function(self):
        """TaskGraph: ensure no reruns if argument is a function."""
        global _append_val

        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        target_path = os.path.join(self.workspace_dir, 'testfile.txt')
        task_graph.add_task(
            func=_call_it,
            args=(_append_val, target_path, 1),
            target_path_list=[target_path],
            ignore_path_list=[target_path],
            task_name='first _call_it')
        task_graph.close()
        task_graph.join()
        del task_graph

        # this causes the address to change
        def _append_val(path, *val):
            """Append a `val` to file at `path`."""
            with open(path, 'a') as target_file:
                for v in val:
                    target_file.write(str(v))

        task_graph = taskgraph.TaskGraph(self.workspace_dir, 1)
        target_path = os.path.join(self.workspace_dir, 'testfile.txt')
        task_graph.add_task(
            func=_call_it,
            args=(_append_val, target_path, 1),
            target_path_list=[target_path],
            ignore_path_list=[target_path],
            task_name='second _call_it')
        task_graph.close()
        task_graph.join()

        with open(target_path, 'r') as target_file:
            result = target_file.read()

        # the second call shouldn't happen
        self.assertEqual(result, '1')

    def test_unix_path_repeated_function(self):
        """TaskGraph: ensure no reruns if path is unix style."""
        global _append_val

        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1)
        target_dir = self.workspace_dir + '/foo/bar/rad/'
        os.makedirs(target_dir)
        target_path = target_dir + '/testfile.txt'
        task_graph.add_task(
            func=_call_it,
            args=(_append_val, target_path, 1),
            target_path_list=[target_path],
            task_name='first _call_it')
        task_graph.close()
        task_graph.join()
        del task_graph

        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1)
        task_graph.add_task(
            func=_call_it,
            args=(_append_val, target_path, 1),
            target_path_list=[target_path],
            task_name='second _call_it')
        task_graph.close()
        task_graph.join()

        with open(target_path, 'r') as target_file:
            result = target_file.read()

        # the second call shouldn't happen
        self.assertEqual(result, '1')

    def test_very_long_string(self):
        """TaskGraph: ensure that long strings don't case an OSError."""
        from taskgraph.Task import _get_file_stats
        # this is a list with two super long strings to try to trick some
        # os function into thinking it's a path.
        base_value = [
            'c:' + r'\\\\\\\\x\\\\\\\\'*2**10 + 'foo',
            'wfeji3223j8923j9' * 2**10]
        self.assertEqual(
            list(_get_file_stats(base_value, 'sizetimestamp', [], True)), [])

    def test_same_contents_duplicate_call(self):
        """TaskGraph: test that same contents copy target path."""
        base_file_path = os.path.join(self.workspace_dir, 'base.txt')
        with open(base_file_path, 'w') as base_file:
            base_file.write('xxx')
        base2_file_path = os.path.join(self.workspace_dir, 'base2.txt')
        shutil.copyfile(base_file_path, base2_file_path)

        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        target_path = os.path.join(self.workspace_dir, 'testfile.txt')
        task_graph.add_task(
            func=_copy_file_once,
            args=(base_file_path, target_path),
            target_path_list=[target_path],
            copy_duplicate_artifact=True,
            hash_algorithm='md5',
            task_name='first _copy_file_once')

        task_graph.close()
        task_graph.join()
        del task_graph

        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        alt_target_path = os.path.join(self.workspace_dir, 'alt_testfile.txt')
        task_graph.add_task(
            func=_copy_file_once,
            args=(base2_file_path, alt_target_path),
            target_path_list=[alt_target_path],
            copy_duplicate_artifact=True,
            hash_algorithm='md5',
            task_name='second _copy_file_once')

        task_graph.close()
        task_graph.join()

        with open(target_path, 'r') as target_file:
            contents = target_file.read()
        self.assertEqual(contents, 'xxx')

        with open(alt_target_path, 'r') as alt_target_file:
            alt_contents = alt_target_file.read()
        self.assertEqual(contents, alt_contents)

    def test_duplicate_call(self):
        """TaskGraph: test that duplicate calls copy target path."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        target_path = os.path.join(self.workspace_dir, 'testfile.txt')
        task_graph.add_task(
            func=_create_file_once,
            args=(target_path, 'test'),
            target_path_list=[target_path],
            copy_duplicate_artifact=True,
            task_name='first _create_file_once')

        task_graph.close()
        task_graph.join()
        del task_graph

        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        alt_target_path = os.path.join(self.workspace_dir, 'alt_testfile.txt')
        task_graph.add_task(
            func=_create_file_once,
            args=(alt_target_path, 'test'),
            target_path_list=[alt_target_path],
            copy_duplicate_artifact=True,
            task_name='second _create_file_once')

        task_graph.close()
        task_graph.join()

        with open(target_path, 'r') as target_file:
            contents = target_file.read()
        self.assertEqual(contents, 'test')

        with open(alt_target_path, 'r') as alt_target_file:
            alt_contents = alt_target_file.read()
        self.assertEqual(contents, alt_contents)

    def test_duplicate_call_modify_timestamp(self):
        """TaskGraph: test that duplicate call modified stamp recompute."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        target_path = os.path.join(self.workspace_dir, 'testfile.txt')
        task_graph.add_task(
            func=_create_file,
            args=(target_path, 'test'),
            target_path_list=[target_path],
            copy_duplicate_artifact=True,
            task_name='first _create_file')
        task_graph.close()
        task_graph.join()
        del task_graph

        with open(target_path, 'w') as target_file:
            target_file.write('test2')
        with open(target_path, 'r') as target_file:
            contents = target_file.read()
        self.assertEqual(contents, 'test2')

        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        task_graph.add_task(
            func=_create_file,
            args=(target_path, 'test'),
            target_path_list=[target_path],
            copy_duplicate_artifact=True,
            task_name='second _create_file')

        task_graph.close()
        task_graph.join()

        with open(target_path, 'r') as target_file:
            contents = target_file.read()
        self.assertEqual(contents, 'test')

    def test_different_target_path_list(self):
        """TaskGraph: duplicate calls with different targets should fail."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        target_path = os.path.join(self.workspace_dir, 'testfile.txt')
        task_graph.add_task(
            func=_create_list_on_disk,
            args=('test', 1, target_path),
            target_path_list=[target_path],
            task_name='first _create_list_on_disk')

        with self.assertRaises(RuntimeError):
            # make the same call but with different target path list
            task_graph.add_task(
                func=_create_list_on_disk,
                args=('test', 1, target_path),
                target_path_list=[target_path, 'test.txt'],
                task_name='first _create_list_on_disk')

        task_graph.close()
        task_graph.join()

    def test_terminated_taskgraph(self):
        """TaskGraph: terminated task graph raises exception correctly."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 4)
        _ = task_graph.add_task(func=_div_by_zero)
        with self.assertRaises(ZeroDivisionError):
            task_graph.join()

        with self.assertRaises(RuntimeError) as cm:
            _ = task_graph.add_task(func=_div_by_zero)
        expected_message = "add_task when Taskgraph is terminated"
        actual_message = str(cm.exception)
        self.assertTrue(expected_message in actual_message, actual_message)

        task_graph.close()
        # try closing twice just to mess with coverage
        task_graph.close()

    def test_type_list_error(self):
        """TaskGraph: Task not passed to dependent task list."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1)
        target_path = os.path.join(self.workspace_dir, 'testfile.txt')
        with self.assertRaises(ValueError) as cm:
            task_graph.add_task(
                func=_create_list_on_disk,
                args=('test', 1, target_path),
                target_path_list=[target_path],
                dependent_task_list=[target_path],
                task_name='first _create_list_on_disk')
        expected_message = (
            "Objects passed to dependent task list that are not tasks")
        actual_message = str(cm.exception)
        self.assertTrue(expected_message in actual_message, actual_message)

    def test_target_list_error(self):
        """TaskGraph: Path not passed to target list."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1)
        target_path = os.path.join(self.workspace_dir, 'testfile.txt')
        with self.assertRaises(ValueError) as cm:
            task_graph.add_task(
                func=_create_list_on_disk,
                args=('test', 1, target_path),
                target_path_list=[1],
                task_name='_create_list_on_disk')
        expected_message = (
            "Values passed to target_path_list are not strings")
        actual_message = str(cm.exception)
        self.assertTrue(expected_message in actual_message, actual_message)

    def test_target_path_missing_file(self):
        """TaskGraph: func runs, but missing target."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1)
        target_path = os.path.join(self.workspace_dir, 'testfile.txt')
        not_target_path = os.path.join(self.workspace_dir, 'not_target.txt')
        with self.assertRaises(RuntimeError) as cm:
            task_graph.add_task(
                func=_create_list_on_disk,
                args=('test', 1, target_path),
                target_path_list=[not_target_path],
                task_name='_create_list_on_disk')
        expected_message = "Missing expected target path results"
        actual_message = str(cm.exception)
        self.assertTrue(expected_message in actual_message, actual_message)

    def test_duplicate_but_different_target(self):
        """TaskGraph: Two tasks that are identical but for target."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, 0)
        base_path = os.path.join(self.workspace_dir, 'base.txt')
        target_a_path = os.path.join(self.workspace_dir, 'testa.txt')
        target_b_path = os.path.join(self.workspace_dir, 'testb.txt')
        target_c_path = os.path.join(self.workspace_dir, 'testc.txt')
        target_d_path = os.path.join(self.workspace_dir, 'testd.txt')

        test_string = 'test string'
        with open(base_path, 'w') as base_file:
            base_file.write(test_string)

        _ = task_graph.add_task(
            func=_copy_two_files_once,
            args=(base_path, target_a_path, target_b_path),
            copy_duplicate_artifact=True,
            hash_algorithm='md5',
            target_path_list=[target_a_path, target_b_path])
        # this task should copy b to c but not a to a.
        _ = task_graph.add_task(
            func=_copy_two_files_once,
            args=(base_path, target_c_path, target_d_path),
            copy_duplicate_artifact=True,
            hash_algorithm='md5',
            target_path_list=[target_c_path, target_d_path])
        task_graph.close()
        task_graph.join()

        for path in (target_a_path, target_b_path, target_c_path):
            with open(path, 'r') as target_file:
                contents = target_file.read()
            self.assertEqual(contents, test_string)

    def test_modifying_functions_with_copy(self):
        """TaskGraph: test with copy artifacts and ignore inputs."""
        n_runs_a = 0
        n_runs_b = 0

        a_path = os.path.join(self.workspace_dir, 'a.txt')
        b_path = os.path.join(self.workspace_dir, 'b.txt')
        volatile_path = os.path.join(self.workspace_dir, 'volatile.txt')
        d_path = os.path.join(self.workspace_dir, 'd.txt')

        b_path_suffix = os.path.join(self.workspace_dir, 'b_suffix.txt')
        volatile_path_suffix = os.path.join(
            self.workspace_dir, 'volatile_suffix.txt')
        d_path_suffix = os.path.join(self.workspace_dir, 'd_suffix.txt')

        with open(a_path, 'w') as a_file:
            a_file.write('a file')

        def run_a_batch(a_path, b_path, volatile_path, d_path):
            def _a(a_path, target_path):
                nonlocal n_runs_a
                n_runs_a += 1
                if not os.path.exists(a_path):
                    raise RuntimeError("a_path doesn't exist")
                with open(target_path, 'w') as target_file:
                    target_file.write('_a result')

            def _b(b_path, volatile_path, target_path):
                nonlocal n_runs_b
                n_runs_b += 1
                if not os.path.exists(a_path):
                    raise RuntimeError("a_path path doesn't exist")
                with open(volatile_path, 'w') as volitile_file:
                    volitile_file.write('_b volatile')
                with open(target_path, 'w') as target_file:
                    target_file.write('_b result')

            task_graph = taskgraph.TaskGraph(self.workspace_dir, -1, 0)
            task_a = task_graph.add_task(
                func=_a,
                args=(a_path, b_path),
                target_path_list=[b_path],
                hash_algorithm='md5',
                copy_duplicate_artifact=True,
                task_name='_a task')
            _ = task_graph.add_task(
                func=_b,
                args=(b_path, volatile_path, d_path),
                target_path_list=[d_path],
                ignore_path_list=[volatile_path],
                hash_algorithm='md5',
                copy_duplicate_artifact=True,
                dependent_task_list=[task_a],
                task_name='_b task')
            task_graph.join()
            task_graph.close()
            del task_graph

        run_a_batch(a_path, b_path, volatile_path, d_path)
        run_a_batch(a_path, b_path, volatile_path, d_path)
        run_a_batch(a_path, b_path_suffix, volatile_path_suffix, d_path_suffix)

        self.assertTrue(n_runs_a == 1)
        self.assertTrue(n_runs_b == 1)

    def test_expected_path_list(self):
        """TaskGraph: test expected path list matches actual path list."""
        def _create_file(target_path, content):
            with open(target_path, 'w') as target_file:
                target_file.write(content)

        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1, 0)
        # note it is important this is a relative path that does not
        # contain the drive letter on Windows.
        absolute_target_file_path = os.path.join(
            self.workspace_dir, 'a.txt')
        relative_path = os.path.relpath(absolute_target_file_path)

        _ = task_graph.add_task(
           func=_create_file,
           args=(relative_path, 'test value'),
           target_path_list=[relative_path],
           task_name='create file')

        task_graph.close()
        task_graph.join()
        del task_graph

        self.assertTrue('Ran without crashing!')

    def test_kwargs_hashed(self):
        """TaskGraph: ensure kwargs are considered in determining id hash."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1, 0)

        task_a = task_graph.add_task(
            func=_noop_function,
            kwargs={
                'content': ['this value: a']},
            task_name='noop a')

        task_b = task_graph.add_task(
            func=_noop_function,
            kwargs={
                'content': ['this value b']},
            task_name='noop b')

        task_graph.close()
        task_graph.join()
        del task_graph

        self.assertNotEqual(
            task_a._task_id_hash, task_b._task_id_hash,
            "task ids should be different since the kwargs are different")

    def test_same_timestamp_and_value(self):
        """TaskGraph: ensure identical files but filename are noticed."""
        task_graph = taskgraph.TaskGraph(self.workspace_dir, -1, 0)

        file_a_path = os.path.join(self.workspace_dir, 'file_a.txt')
        file_b_path = os.path.join(self.workspace_dir, 'file_b.txt')

        with open(file_a_path, 'w') as file_a:
            file_a.write('a')
        with open(file_b_path, 'w') as file_b:
            file_b.write('a')

        os.utime(file_a_path, (0, 0))
        os.utime(file_b_path, (0, 0))

        task_a = task_graph.add_task(
            func=_noop_function,
            kwargs={
                'path': file_a_path},
            task_name='noop a')

        task_b = task_graph.add_task(
            func=_noop_function,
            kwargs={
                'path': file_b_path},
            task_name='noop b')

        task_graph.close()
        task_graph.join()
        del task_graph

        self.assertNotEqual(
            task_a._task_id_hash, task_b._task_id_hash,
            "task ids should be different since the filenames are different")


def Fail(n_tries, result_path):
    """Create a function that fails after `n_tries`."""
    def fail_func():
        fail_func._n_tries -= 1
        if fail_func._n_tries > 0:
            raise ValueError("Fail %d more times", fail_func._n_tries)
        with open(result_path, 'w') as result_file:
            result_file.write("finished!")
    fail_func._n_tries = n_tries

    return fail_func
