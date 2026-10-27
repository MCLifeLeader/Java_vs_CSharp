"""Local JDK fallback never changes global configuration or accepts another build."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from controller.toolchain import activate_toolchain


class ToolchainTests(unittest.TestCase):
    def local_jdk(self,root,release='IMPLEMENTOR="Microsoft"\nJAVA_RUNTIME_VERSION="25.0.3+9-LTS"\n'):
        home=Path(root)/'.work/tools/jdk-25.0.3+9'
        (home/'bin').mkdir(parents=True)
        for name in ('java','javac'):
            (home/'bin'/(name+('.exe' if os.name=='nt' else ''))).write_bytes(b'fixture')
        (home/'release').write_text(release)
        return home
    def test_existing_tools_preserve_process_environment(self):
        with patch('controller.toolchain.shutil.which',return_value='existing'),patch.dict(os.environ,{'JAVA_HOME':'before','PATH':'before'},clear=True):
            self.assertFalse(activate_toolchain('.') ['activated'])
            self.assertEqual('before',os.environ['JAVA_HOME'])
            self.assertEqual('before',os.environ['PATH'])
    def test_missing_tools_report_bootstrap_without_mutation(self):
        with tempfile.TemporaryDirectory() as root,patch('controller.toolchain.shutil.which',return_value=None),patch.dict(os.environ,{'PATH':'before'},clear=True):
            self.assertFalse(activate_toolchain(root)['activated'])
            self.assertNotIn('JAVA_HOME',os.environ)
            self.assertEqual('before',os.environ['PATH'])
    def test_exact_local_build_activates_only_current_environment(self):
        with tempfile.TemporaryDirectory() as root,patch('controller.toolchain.shutil.which',return_value=None),patch.dict(os.environ,{'PATH':'before'},clear=True):
            home=self.local_jdk(root).resolve()
            self.assertTrue(activate_toolchain(root)['activated'])
            self.assertEqual(str(home),os.environ['JAVA_HOME'])
            self.assertEqual(str(home/'bin')+os.pathsep+'before',os.environ['PATH'])
    def test_wrong_local_jdk_is_rejected(self):
        with tempfile.TemporaryDirectory() as root,patch('controller.toolchain.shutil.which',return_value=None):
            self.local_jdk(root,'IMPLEMENTOR="Other"\nJAVA_RUNTIME_VERSION="25.0.3+9-LTS"\n')
            with self.assertRaisesRegex(ValueError,'identity'):
                activate_toolchain(root)


if __name__=='__main__':
    unittest.main()
