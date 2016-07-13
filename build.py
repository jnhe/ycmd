#!/usr/bin/env python

# Passing an environment variable containing unicode literals to a subprocess
# on Windows and Python2 raises a TypeError. Since there is no unicode
# string in this script, we don't import unicode_literals to avoid the issue.
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

from distutils import sysconfig
from shutil import rmtree
from tempfile import mkdtemp
import errno
import multiprocessing
import os
import os.path as p
import platform
import re
import shlex
import subprocess
import sys

PY_MAJOR, PY_MINOR = sys.version_info[ 0 : 2 ]
if not ( ( PY_MAJOR == 2 and PY_MINOR >= 6 ) or
         ( PY_MAJOR == 3 and PY_MINOR >= 3 ) or
         PY_MAJOR > 3 ):
  sys.exit( 'ycmd requires Python >= 2.6 or >= 3.3; '
            'your version of Python is ' + sys.version )

DIR_OF_THIS_SCRIPT = p.dirname( p.abspath( __file__ ) )
DIR_OF_THIRD_PARTY = p.join( DIR_OF_THIS_SCRIPT, 'third_party' )

for folder in os.listdir( DIR_OF_THIRD_PARTY ):
  abs_folder_path = p.join( DIR_OF_THIRD_PARTY, folder )
  if p.isdir( abs_folder_path ) and not os.listdir( abs_folder_path ):
    sys.exit( 'Some folders in ' + DIR_OF_THIRD_PARTY + ' are empty; '
              'you probably forgot to run:'
              '\n\tgit submodule update --init --recursive\n\n' )

sys.path.insert( 1, p.abspath( p.join( DIR_OF_THIRD_PARTY, 'argparse' ) ) )

import argparse

NO_DYNAMIC_PYTHON_ERROR = (
  'ERROR: found static Python library ({library}) but a dynamic one is '
  'required. You must use a Python compiled with the {flag} flag. '
  'If using pyenv, you need to run the command:\n'
  '  export PYTHON_CONFIGURE_OPTS="{flag}"\n'
  'before installing a Python version.' )
NO_PYTHON_LIBRARY_ERROR = 'ERROR: unable to find an appropriate Python library.'

# Regular expressions used to find static and dynamic Python libraries.
# Notes:
#  - Python 3 library name may have an 'm' suffix on Unix platforms, for
#    instance libpython3.3m.so;
#  - the linker name (the soname without the version) does not always
#    exist so we look for the versioned names too;
#  - on Windows, the .lib extension is used instead of the .dll one. See
#    http://xenophilia.org/winvunix.html to understand why.
STATIC_PYTHON_LIBRARY_REGEX = '^libpython{major}\.{minor}m?\.a$'
DYNAMIC_PYTHON_LIBRARY_REGEX = """
  ^(?:
  # Linux, BSD
  libpython{major}\.{minor}m?\.so(\.\d+)*|
  # OS X
  libpython{major}\.{minor}m?\.dylib|
  # Windows
  python{major}{minor}\.lib
  )$
"""


def OnMac():
  return platform.system() == 'Darwin'


def OnWindows():
  return platform.system() == 'Windows'


def OnTravisOrAppVeyor():
  return 'CI' in os.environ


# On Windows, distutils.spawn.find_executable only works for .exe files
# but .bat and .cmd files are also executables, so we use our own
# implementation.
def FindExecutable( executable ):
  # Executable extensions used on Windows
  WIN_EXECUTABLE_EXTS = [ '.exe', '.bat', '.cmd' ]

  paths = os.environ[ 'PATH' ].split( os.pathsep )
  base, extension = os.path.splitext( executable )

  if OnWindows() and extension.lower() not in WIN_EXECUTABLE_EXTS:
    extensions = WIN_EXECUTABLE_EXTS
  else:
    extensions = ['']

  for extension in extensions:
    executable_name = executable + extension
    if not os.path.isfile( executable_name ):
      for path in paths:
        executable_path = os.path.join(path, executable_name )
        if os.path.isfile( executable_path ):
          return executable_path
    else:
      return executable_name
  return None


def PathToFirstExistingExecutable( executable_name_list ):
  for executable_name in executable_name_list:
    path = FindExecutable( executable_name )
    if path:
      return path
  return None


def NumCores():
  ycm_cores = os.environ.get( 'YCM_CORES' )
  if ycm_cores:
    return int( ycm_cores )
  try:
    return multiprocessing.cpu_count()
  except NotImplementedError:
    return 1


def CheckDeps():
  if not PathToFirstExistingExecutable( [ 'cmake' ] ):
    sys.exit( 'Please install CMake and retry.')


# Shamelessly stolen from https://gist.github.com/edufelipe/1027906
def CheckOutput( *popen_args, **kwargs ):
  """Run command with arguments and return its output as a byte string.
  Backported from Python 2.7."""

  process = subprocess.Popen( stdout=subprocess.PIPE, *popen_args, **kwargs )
  output, unused_err = process.communicate()
  retcode = process.poll()
  if retcode:
    command = kwargs.get( 'args' )
    if command is None:
      command = popen_args[ 0 ]
    error = subprocess.CalledProcessError( retcode, command )
    error.output = output
    raise error
  return output


def GetPossiblePythonLibraryDirectories():
  library_dir = p.dirname( sysconfig.get_python_lib( standard_lib = True ) )
  if OnWindows():
    return [ p.join( library_dir, 'libs' ) ]
  # On pyenv, there is no Python dynamic library in the directory returned by
  # the LIBPL variable. Such library is located in the parent folder of the
  # standard Python library modules.
  return [ sysconfig.get_config_var( 'LIBPL' ), library_dir ]


def FindPythonLibraries():
  include_dir = sysconfig.get_python_inc()
  library_dirs = GetPossiblePythonLibraryDirectories()

  # Since ycmd is compiled as a dynamic library, we can't link it to a Python
  # static library. If we try, the following error will occur on Mac:
  #
  #   Fatal Python error: PyThreadState_Get: no current thread
  #
  # while the error happens during linking on Linux and looks something like:
  #
  #   relocation R_X86_64_32 against `a local symbol' can not be used when
  #   making a shared object; recompile with -fPIC
  #
  # On Windows, the Python library is always a dynamic one (an import library to
  # be exact). To obtain a dynamic library on other platforms, Python must be
  # compiled with the --enable-shared flag on Linux or the --enable-framework
  # flag on Mac.
  #
  # So we proceed like this:
  #  - look for a dynamic library and return its path;
  #  - if a static library is found instead, raise an error with instructions
  #    on how to build Python as a dynamic library.
  #  - if no libraries are found, raise a generic error.
  dynamic_name = re.compile( DYNAMIC_PYTHON_LIBRARY_REGEX.format(
    major = PY_MAJOR, minor = PY_MINOR ), re.X )
  static_name = re.compile( STATIC_PYTHON_LIBRARY_REGEX.format(
    major = PY_MAJOR, minor = PY_MINOR ), re.X )
  static_libraries = []

  for library_dir in library_dirs:
    # Files are sorted so that we found the non-versioned Python library before
    # the versioned one.
    for filename in sorted( os.listdir( library_dir ) ):
      if dynamic_name.match( filename ):
        return p.join( library_dir, filename ), include_dir

      if static_name.match( filename ):
        static_libraries.append( p.join( library_dir, filename ) )

  if static_libraries and not OnWindows():
    dynamic_flag = ( '--enable-framework' if OnMac() else
                     '--enable-shared' )
    sys.exit( NO_DYNAMIC_PYTHON_ERROR.format( library = static_libraries[ 0 ],
                                              flag = dynamic_flag ) )

  sys.exit( NO_PYTHON_LIBRARY_ERROR )


def CustomPythonCmakeArgs():
  # The CMake 'FindPythonLibs' Module does not work properly.
  # So we are forced to do its job for it.
  print( 'Searching Python {major}.{minor} libraries...'.format(
    major = PY_MAJOR, minor = PY_MINOR ) )

  python_library, python_include = FindPythonLibraries()

  print( 'Found Python library: {0}'.format( python_library ) )
  print( 'Found Python headers folder: {0}'.format( python_include ) )

  return [
    '-DPYTHON_LIBRARY={0}'.format( python_library ),
    '-DPYTHON_INCLUDE_DIR={0}'.format( python_include )
  ]


def GetGenerator( args ):
  if OnWindows():
    if args.msvc == 14:
      generator = 'Visual Studio 14'
    elif args.msvc == 12:
      generator = 'Visual Studio 12'
    else:
      generator = 'Visual Studio 11'

    if platform.architecture()[ 0 ] == '64bit':
      generator = generator + ' Win64'
    return generator

  if PathToFirstExistingExecutable( ['ninja'] ):
    return 'Ninja'
  return 'Unix Makefiles'


def ParseArguments():
  parser = argparse.ArgumentParser()
  parser.add_argument( '--clang-completer', action = 'store_true',
                       help = 'Build C-family semantic completion engine.' )
  parser.add_argument( '--system-libclang', action = 'store_true',
                       help = 'Use system libclang instead of downloading one '
                       'from llvm.org. NOT RECOMMENDED OR SUPPORTED!' )
  parser.add_argument( '--omnisharp-completer', action = 'store_true',
                       help = 'Build C# semantic completion engine.' )
  parser.add_argument( '--gocode-completer', action = 'store_true',
                       help = 'Build Go semantic completion engine.' )
  parser.add_argument( '--racer-completer', action = 'store_true',
                       help = 'Build rust semantic completion engine.' )
  parser.add_argument( '--system-boost', action = 'store_true',
                       help = 'Use the system boost instead of bundled one. '
                       'NOT RECOMMENDED OR SUPPORTED!')
  parser.add_argument( '--msvc', type = int, choices = [ 11, 12, 14 ],
                       default = 14, help = 'Choose the Microsoft Visual '
                       'Studio version (default: %(default)s).' )
  parser.add_argument( '--tern-completer',
                       action = 'store_true',
                       help   = 'Enable tern javascript completer' ),
  parser.add_argument( '--all',
                       action = 'store_true',
                       help   = 'Enable all supported completers',
                       dest   = 'all_completers' )
  parser.add_argument( '--enable-debug',
                       action = 'store_true',
                       help   = 'For developers: build ycm_core library with '
                                'debug symbols' )

  args = parser.parse_args()

  if ( args.system_libclang and
       not args.clang_completer and
       not args.all_completers ):
    sys.exit( "You can't pass --system-libclang without also passing "
              "--clang-completer or --all as well." )
  return args


def GetCmakeArgs( parsed_args ):
  cmake_args = []
  if parsed_args.clang_completer or parsed_args.all_completers:
    cmake_args.append( '-DUSE_CLANG_COMPLETER=ON' )

  if parsed_args.system_libclang:
    cmake_args.append( '-DUSE_SYSTEM_LIBCLANG=ON' )

  if parsed_args.system_boost:
    cmake_args.append( '-DUSE_SYSTEM_BOOST=ON' )

  if parsed_args.enable_debug:
    cmake_args.append( '-DCMAKE_BUILD_TYPE=Debug' )

  use_python2 = 'ON' if PY_MAJOR == 2 else 'OFF'
  cmake_args.append( '-DUSE_PYTHON2=' + use_python2 )

  extra_cmake_args = os.environ.get( 'EXTRA_CMAKE_ARGS', '' )
  # We use shlex split to properly parse quoted CMake arguments.
  cmake_args.extend( shlex.split( extra_cmake_args ) )
  return cmake_args


def RunYcmdTests( build_dir ):
  tests_dir = p.join( build_dir, 'ycm', 'tests' )
  os.chdir( tests_dir )
  new_env = os.environ.copy()

  if OnWindows():
    # We prepend the folder of the ycm_core_tests executable to the PATH
    # instead of overwriting it so that the executable is able to find the
    # python35.dll library.
    new_env[ 'PATH' ] = DIR_OF_THIS_SCRIPT + ';' + new_env[ 'PATH' ]
  else:
    new_env[ 'LD_LIBRARY_PATH' ] = DIR_OF_THIS_SCRIPT

  subprocess.check_call( p.join( tests_dir, 'ycm_core_tests' ), env = new_env )


# On Windows, if the ycmd library is in use while building it, a LNK1104
# fatal error will occur during linking. Exit the script early with an
# error message if this is the case.
def ExitIfYcmdLibInUseOnWindows():
  if not OnWindows():
    return

  ycmd_library = p.join( DIR_OF_THIS_SCRIPT, 'ycm_core.pyd' )

  if not p.exists( ycmd_library ):
    return

  try:
    open( p.join( ycmd_library ), 'a' ).close()
  except IOError as error:
    if error.errno == errno.EACCES:
      sys.exit( 'ERROR: ycmd library is currently in use. '
                'Stop all ycmd instances before compilation.' )


def BuildYcmdLib( args ):
  build_dir = mkdtemp( prefix = 'ycm_build.' )

  try:
    full_cmake_args = [ '-G', GetGenerator( args ) ]
    full_cmake_args.extend( CustomPythonCmakeArgs() )
    full_cmake_args.extend( GetCmakeArgs( args ) )
    full_cmake_args.append( p.join( DIR_OF_THIS_SCRIPT, 'cpp' ) )

    os.chdir( build_dir )
    subprocess.check_call( [ 'cmake' ] + full_cmake_args )

    build_target = ( 'ycm_core' if 'YCM_TESTRUN' not in os.environ else
                     'ycm_core_tests' )

    build_command = [ 'cmake', '--build', '.', '--target', build_target ]
    if OnWindows():
      config = 'Debug' if args.enable_debug else 'Release'
      build_command.extend( [ '--config', config ] )
    else:
      build_command.extend( [ '--', '-j', str( NumCores() ) ] )

    subprocess.check_call( build_command )

    if 'YCM_TESTRUN' in os.environ:
      RunYcmdTests( build_dir )
  finally:
    os.chdir( DIR_OF_THIS_SCRIPT )
    rmtree( build_dir, ignore_errors = OnTravisOrAppVeyor() )


def BuildOmniSharp():
  build_command = PathToFirstExistingExecutable(
    [ 'msbuild', 'msbuild.exe', 'xbuild' ] )
  if not build_command:
    sys.exit( 'msbuild or xbuild is required to build Omnisharp' )

  os.chdir( p.join( DIR_OF_THIS_SCRIPT, 'third_party', 'OmniSharpServer' ) )
  subprocess.check_call( [ build_command, '/property:Configuration=Release' ] )


def BuildGoCode():
  if not FindExecutable( 'go' ):
    sys.exit( 'go is required to build gocode' )

  os.chdir( p.join( DIR_OF_THIS_SCRIPT, 'third_party', 'gocode' ) )
  subprocess.check_call( [ 'go', 'build' ] )
  os.chdir( p.join( DIR_OF_THIS_SCRIPT, 'third_party', 'godef' ) )
  subprocess.check_call( [ 'go', 'build' ] )


def BuildRacerd():
  """
  Build racerd. This requires a reasonably new version of rustc/cargo.
  """
  if not FindExecutable( 'cargo' ):
    sys.exit( 'cargo is required for the rust completer' )

  os.chdir( p.join( DIR_OF_THIRD_PARTY, 'racerd' ) )
  args = [ 'cargo', 'build' ]
  # We don't use the --release flag on Travis/AppVeyor because it makes building
  # racerd 2.5x slower and we don't care about the speed of the produced racerd.
  if not OnTravisOrAppVeyor():
    args.append( '--release' )
  subprocess.check_call( args )


def SetUpTern():
  paths = {}
  for exe in [ 'node', 'npm' ]:
    path = FindExecutable( exe )
    if not path:
      sys.exit( '"' + exe + '" is required to set up ternjs' )
    else:
      paths[ exe ] = path

  # We install Tern into a runtime directory. This allows us to control
  # precisely the version (and/or git commit) that is used by ycmd.  We use a
  # separate runtime directory rather than a submodule checkout directory
  # because we want to allow users to install third party plugins to
  # node_modules of the Tern runtime.  We also want to be able to install our
  # own plugins to improve the user experience for all users.
  #
  # This is not possible if we use a git submodule for Tern and simply run 'npm
  # install' within the submodule source directory, as subsequent 'npm install
  # tern-my-plugin' will (heinously) install another (arbitrary) version of Tern
  # within the Tern source tree (e.g. third_party/tern/node_modules/tern. The
  # reason for this is that the plugin that gets installed has "tern" as a
  # dependency, and npm isn't smart enough to know that you're installing
  # *within* the Tern distribution. Or it isn't intended to work that way.
  #
  # So instead, we have a package.json within our "Tern runtime" directory
  # (third_party/tern_runtime) that defines the packages that we require,
  # including Tern and any plugins which we require as standard.
  os.chdir( p.join( DIR_OF_THIS_SCRIPT, 'third_party', 'tern_runtime' ) )
  subprocess.check_call( [ paths[ 'npm' ], 'install', '--production' ] )


def WritePythonUsedDuringBuild():
  path = p.join( DIR_OF_THIS_SCRIPT, 'PYTHON_USED_DURING_BUILDING' )
  with open( path, 'w' ) as f:
    f.write( sys.executable )


def Main():
  CheckDeps()
  args = ParseArguments()
  ExitIfYcmdLibInUseOnWindows()
  BuildYcmdLib( args )
  if args.omnisharp_completer or args.all_completers:
    BuildOmniSharp()
  if args.gocode_completer or args.all_completers:
    BuildGoCode()
  if args.tern_completer or args.all_completers:
    SetUpTern()
  if args.racer_completer or args.all_completers:
    BuildRacerd()
  WritePythonUsedDuringBuild()


if __name__ == '__main__':
  Main()
