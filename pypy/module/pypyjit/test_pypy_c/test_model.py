import sys
import subprocess
import py
from pypy.tool.udir import udir
from pypy.tool import logparser
from pypy.module.pypyjit.test_pypy_c.model import Log

class BaseTestPyPyC(object):
    def setup_class(cls):
        if '__pypy__' not in sys.builtin_module_names:
            py.test.skip("must run this test with pypy")
        if not sys.pypy_translation_info['translation.jit']:
            py.test.skip("must give a pypy-c with the jit enabled")
        cls.tmpdir = udir.join('test-pypy-jit')
        cls.tmpdir.ensure(dir=True)

    def setup_method(self, meth):
        self.filepath = self.tmpdir.join(meth.im_func.func_name + '.py')

    def run(self, func, threshold=1000):
        # write the snippet
        with self.filepath.open("w") as f:
            f.write(str(py.code.Source(func)) + "\n")
            f.write("%s()\n" % func.func_name)
        #
        # run a child pypy-c with logging enabled
        logfile = self.filepath.new(ext='.log')
        env={'PYPYLOG': 'jit-log-opt,jit-summary:' + str(logfile)}
        cmdline = [sys.executable,
                   '--jit', 'threshold=%d' % threshold,
                   str(self.filepath)]
        pipe = subprocess.Popen(cmdline,
                                env=env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        pipe.wait()
        stderr = pipe.stderr.read()
        stdout = pipe.stdout.read()
        assert not stderr
        #
        # parse the JIT log
        rawlog = logparser.parse_log_file(str(logfile))
        rawtraces = logparser.extract_category(rawlog, 'jit-log-opt-')
        log = Log(func, rawtraces)
        return log


class TestLog(object):

    def test_find_chunks_range(self):
        def f():
            a = 0 # ID: myline
            return a
        #
        start_lineno = f.func_code.co_firstlineno
        ids = Log.find_chunks_range(f)
        assert len(ids) == 1
        myline_range = ids['myline']
        assert list(myline_range) == range(start_lineno+1, start_lineno+2)

    def test_find_chunks(self):
        def f():
            i = 0
            x = 0
            z = x + 3 # ID: myline
            return z
        #
        chunks = Log.find_chunks(f)
        assert len(chunks) == 1
        myline = chunks['myline']
        opcodes_names = [opcode.__class__.__name__ for opcode in myline]
        assert opcodes_names == ['LOAD_FAST', 'LOAD_CONST', 'BINARY_ADD', 'STORE_FAST']

class TestRunPyPyC(BaseTestPyPyC):

    def test_parse_jitlog(self):
        py.test.skip('in-progress')
        def f():
            i = 0
            while i < 1003: # default threshold is 10
                i += 1 # ID: increment
            return i
        #
        log = self.run(f)