#!/usr/bin/env python
# encoding: utf-8
'''
Collector -- Crash processing client

Provide process and class level interfaces to process crash information with
a remote server.

@author:     Christian Holler (:decoder)

@license:

This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.

@contact:    choller@mozilla.com
'''

# Ensure print() compatibility with Python 3
from __future__ import print_function

import sys
import os
import json
import base64
import argparse
import hashlib
import platform
import requests
from tempfile import mkstemp
from zipfile import ZipFile

BASE_DIR = os.path.dirname(os.path.realpath(__file__))
FTB_PATH = os.path.abspath(os.path.join(BASE_DIR, ".."))
sys.path += [FTB_PATH]

from FTB.ProgramConfiguration import ProgramConfiguration
from FTB.Running.AutoRunner import AutoRunner
from FTB.Signatures.CrashSignature import CrashSignature
from FTB.Signatures.CrashInfo import CrashInfo
from FTB.ConfigurationFiles import ConfigurationFiles


__all__ = []
__version__ = 0.1
__date__ = '2014-10-01'
__updated__ = '2014-10-01'

def remote_checks(f):
    'Decorator to perform error checks before using remote features'
    def decorator(self, *args, **kwargs):
        if not self.serverHost:
            raise RuntimeError("Must specify serverHost (configuration property: serverhost) to use remote features.")
        if not self.serverHost:
            raise RuntimeError("Must specify serverAuthToken (configuration property: serverauthtoken) to use remote features.")
        if not self.tool:
            raise RuntimeError("Must specify tool (configuration property: tool) to use remote features.")
        return f(self, *args, **kwargs)
    return decorator

def signature_checks(f):
    'Decorator to perform error checks before using signature features'
    def decorator(self, *args, **kwargs):
        if not self.sigCacheDir:
            raise RuntimeError("Must specify sigCacheDir (configuration property: sigdir) to use signatures.")
        return f(self, *args, **kwargs)
    return decorator

class Collector():
    def __init__(self, sigCacheDir=None, serverHost=None, serverPort=None,
                 serverProtocol=None, serverAuthToken=None,
                 clientId=None, tool=None):
        '''
        Initialize the Collector. This constructor will also attempt to read
        a configuration file to populate any missing properties that have not
        been passed to this constructor. 
        
        @type sigCacheDir: string
        @param sigCacheDir: Directory to be used for caching signatures
        @type serverHost: string
        @param serverHost: Server host to contact for refreshing signatures
        @type serverPort: int
        @param serverPort: Server port to use when contacting server
        @type serverAuthToken: string
        @param serverAuthToken: Token for server authentication
        @type clientId: string
        @param clientId: Client ID stored in the server when submitting issues
        @type tool: string
        @param tool: Name of the tool that found this issue
        '''
        self.sigCacheDir = sigCacheDir
        self.serverHost = serverHost
        self.serverPort = serverPort
        self.serverProtocol = serverProtocol
        self.serverAuthToken = serverAuthToken
        self.clientId = clientId
        self.tool = tool
        
        # Now search for the global configuration file. If it exists, read its contents
        # and set all Collector settings that haven't been explicitely set by the user.
        globalConfigFile = os.path.join(os.path.expanduser("~"), ".fuzzmanagerconf")
        if os.path.exists(globalConfigFile):
            configInstance = ConfigurationFiles([ globalConfigFile ])
            globalConfig = configInstance.mainConfig
            
            if self.sigCacheDir == None and "sigdir" in globalConfig:
                self.sigCacheDir = globalConfig["sigdir"]
            
            if self.serverHost == None and "serverhost" in globalConfig:
                self.serverHost = globalConfig["serverhost"]
                
            if self.serverPort == None and "serverport" in globalConfig:
                self.serverPort = globalConfig["serverport"]
                
            if self.serverProtocol == None and "serverproto" in globalConfig:
                self.serverProtocol = globalConfig["serverproto"]
                
            if self.serverAuthToken == None:
                if "serverauthtoken" in globalConfig:
                    self.serverAuthToken = globalConfig["serverauthtoken"]
                elif "serverauthtokenfile" in globalConfig:
                    with open(globalConfig["serverauthtokenfile"]) as f:
                        self.serverAuthToken = f.read().rstrip()
                
            if self.clientId == None and "clientid" in globalConfig:
                self.clientId = globalConfig["clientid"]
                
            if self.tool == None and "tool" in globalConfig:
                self.tool = globalConfig["tool"]
        
        # Set some defaults that we can't set through default arguments, otherwise
        # they would overwrite configuration file settings
        if self.serverProtocol == None:
            self.serverProtocol = "https"
        
        # Try to be somewhat intelligent about the default port, depending on protocol
        if self.serverPort == None:
            if self.serverProtocol == "https":
                self.serverPort = 433
            else:
                self.serverPort = 80
        
        if self.serverHost != None and self.clientId == None:
            self.clientId = platform.node()
    
    @remote_checks
    @signature_checks
    def refresh(self):
        '''
        Refresh signatures by contacting the server, downloading new signatures
        and invalidating old ones.
        '''     
        url = "%s://%s:%s/crashmanager/files/signatures.zip" % (self.serverProtocol, self.serverHost, self.serverPort)
        
        # We need to use basic authentication here because these files are directly served by the HTTP server
        response = requests.get(url, stream=True, auth=('fuzzmanager', self.serverAuthToken))
        
        if response.status_code != requests.codes["ok"]:
            raise RuntimeError("Server unexpectedly responded with status code %s" % response.status_code)
        
        (zipFileFd, zipFileName) = mkstemp(prefix="fuzzmanager-signatures")
        
        zipFile = os.fdopen(zipFileFd, 'w')
        
        for chunk in response.iter_content(chunk_size=1024): 
            if chunk:
                zipFile.write(chunk)
                zipFile.flush()
        zipFile.close()
        
        with ZipFile(zipFileName, "r") as zipFile:
            if zipFile.testzip() != None:
                raise RuntimeError("Bad CRC for downloaded zipfile %s" % zipFileName)
            
            # Now clean the signature directory, only deleting signatures and metadata
            for sigFile in os.listdir(self.sigCacheDir):
                if sigFile.endswith(".signature") or sigFile.endswith(".metadata"):
                    os.remove(os.path.join(self.sigCacheDir, sigFile))
                else:
                    print("Warning: Skipping deletion of non-signature file: %s" % sigFile, file=sys.stderr)
            
            zipFile.extractall(self.sigCacheDir)
            
        os.remove(zipFileName)

    @remote_checks
    def submit(self, crashInfo, testCase=None, testCaseQuality=0, metaData=None):
        '''
        Submit the given crash information and an optional testcase/metadata
        to the server for processing and storage.
        
        @type crashInfo: CrashInfo
        @param crashInfo: CrashInfo instance obtained from L{CrashInfo.fromRawCrashData}
        
        @type testCase: string
        @param testCase: A file containing a testcase for reproduction
        
        @type testCaseQuality: int
        @param testCaseQuality: A value indicating the quality of the test (less is better)
        
        @type metaData: map
        @param metaData: A map containing arbitrary (application-specific) data which
                         will be stored on the server in JSON format. This metadata is combined
                         with possible metadata stored in the L{ProgramConfiguration} inside crashInfo.
        '''
        url = "%s://%s:%s/crashmanager/rest/crashes/" % (self.serverProtocol, self.serverHost, self.serverPort)
        
        # Serialize our crash information, testcase and metadata into a dictionary to POST
        data = {}
        
        data["rawStdout"] = os.linesep.join(crashInfo.rawStdout)
        data["rawStderr"] = os.linesep.join(crashInfo.rawStderr)
        data["rawCrashData"] = os.linesep.join(crashInfo.rawCrashData)
        
        if testCase:
            (testCaseData, isBinary) = Collector.read_testcase(testCase)
            
            if isBinary:
                testCaseData = base64.b64encode(testCaseData)
                
            data["testcase"] = testCaseData
            data["testcase_isbinary"] = isBinary
            data["testcase_quality"] = testCaseQuality
            data["testcase_ext"] = os.path.splitext(testCase)[1][1:]
            
        data["platform"] = crashInfo.configuration.platform
        data["product"] = crashInfo.configuration.product
        data["os"] = crashInfo.configuration.os
        
        if crashInfo.configuration.version:
            data["product_version"] = crashInfo.configuration.version
        
        data["client"] = self.clientId
        data["tool"] = self.tool
        
        if crashInfo.configuration.metadata or metaData:
            aggrMetaData = {}
            
            if crashInfo.configuration.metadata:
                aggrMetaData.update(crashInfo.configuration.metadata)
            
            if metaData:
                aggrMetaData.update(metaData)
            
            data["metadata"] = json.dumps(aggrMetaData)
        
        if crashInfo.configuration.env:
            data["env"] = json.dumps(crashInfo.configuration.env)
        
        if crashInfo.configuration.args:
            data["args"] = json.dumps(crashInfo.configuration.args)
        
        response = requests.post(url, data, headers=dict(Authorization="Token %s" % self.serverAuthToken))
        
        if response.status_code != requests.codes["created"]:
            raise RuntimeError("Server unexpectedly responded with status code %s" % response.status_code)

    @signature_checks
    def search(self, crashInfo):
        '''
        Searches within the local signature cache directory for a signature matching the
        given crash. 
        
        @type crashInfo: CrashInfo
        @param crashInfo: CrashInfo instance obtained from L{CrashInfo.fromRawCrashData}
        
        @rtype: tuple
        @return: Tuple containing filename of the signature and metadata matching, or None if no match.
        '''
                
        cachedSigFiles = os.listdir(self.sigCacheDir)
        
        for sigFile in cachedSigFiles:
            if not sigFile.endswith('.signature'):
                continue
            
            sigFile = os.path.join(self.sigCacheDir, sigFile)
            if not os.path.isdir(sigFile):
                with open(sigFile) as f:
                    sigData = f.read()
                    crashSig = CrashSignature(sigData)
                    if crashSig.matches(crashInfo):
                        metadataFile = sigFile.replace('.signature', '.metadata')
                        metadata = None
                        if os.path.exists(metadataFile):
                            with open(metadataFile) as m:
                                metadata = json.loads(m.read())
                        
                        return (sigFile, metadata)
        
        return (None, None)
    
    @signature_checks
    def generate(self, crashInfo, forceCrashAddress=None, forceCrashInstruction=None, numFrames=None):
        '''
        Generates a signature in the local cache directory. It will be deleted when L{refresh} is called
        on the same local cache directory.
        
        @type crashInfo: CrashInfo
        @param crashInfo: CrashInfo instance obtained from L{CrashInfo.fromRawCrashData}
        
        @type forceCrashAddress: bool
        @param forceCrashAddress: Force including the crash address into the signature
        @type forceCrashInstruction: bool
        @param forceCrashInstruction: Force including the crash instruction into the signature (GDB only)
        @type numFrames: int
        @param numFrames: How many frames to include in the signature
        
        @rtype: string
        @return: File containing crash signature in JSON format
        '''
        
        sig = crashInfo.createCrashSignature(forceCrashAddress, forceCrashInstruction, numFrames)
        
        if not sig:
            return None
        
        # Write the file to a unique file name
        return self.__store_signature_hashed(sig)
    
    @remote_checks
    def download(self, crashId):
        '''
        Download the testcase for the specified crashId.
        
        @type crashId: int
        @param crashId: ID of the requested crash entry on the server side
        
        @rtype: string
        @return: Name of the file where the test was stored
        '''     
        if not self.serverHost:
            raise RuntimeError("Must specify serverHost to use remote features.")
        
        url = "%s://%s:%s/crashmanager/rest/crashes/%s/" % (self.serverProtocol, self.serverHost, self.serverPort, crashId)
        
        response = requests.get(url, headers=dict(Authorization="Token %s" % self.serverAuthToken))
        
        if response.status_code != requests.codes["ok"]:
            raise RuntimeError("Server unexpectedly responded with status code %s" % response.status_code)
        
        json = response.json()
        
        if not isinstance(json, dict):
            raise RuntimeError("Server sent malformed JSON response: %s" % json)
        
        if not json["testcase"]:
            return None
        
        url = "%s://%s:%s/crashmanager/%s" % (self.serverProtocol, self.serverHost, self.serverPort, json["testcase"])
        response = requests.get(url, auth=('fuzzmanager', self.serverAuthToken))
        
        if response.status_code != requests.codes["ok"]:
            raise RuntimeError("Server unexpectedly responded with status code %s" % response.status_code)
        
        localFile = os.path.basename(json["testcase"])
        with open(localFile, 'w') as f:
            f.write(response.content)
        
        return localFile
            
    def __store_signature_hashed(self, signature):
        '''
        Store a signature, using the sha1 hash hex representation as filename.
        
        @type signature: CrashSignature
        @param signature: CrashSignature to store
        
        @rtype: string
        @return: Name of the file that the signature was written to
        
        '''
        h = hashlib.new('sha1')
        h.update(str(signature))
        sigfile = os.path.join(self.sigCacheDir, h.hexdigest() + ".signature")
        with open(sigfile, 'w') as f:
            f.write(str(signature))
            
        return sigfile
    
    @staticmethod
    def read_testcase(testCase):
        '''
        Read a testcase file, return the content and indicate if it is binary or not.
        
        @type testCase: string
        @param testCase: Filename of the file to open
        
        @rtype: tuple(string, bool)
        @return: Tuple containing the file contents and a boolean indicating if the content is binary
        
        '''
        with open(testCase) as f:
            testCaseData = f.read()
            
            textBytes = bytearray([7,8,9,10,12,13,27]) + bytearray(range(0x20, 0x100))
            isBinary = lambda input: bool(input.translate(None, textBytes))
            
            return (testCaseData, isBinary(testCaseData))

def main(argv=None):
    '''Command line options.'''

    program_name = os.path.basename(sys.argv[0])
    program_version = "v%s" % __version__
    program_build_date = "%s" % __updated__

    program_version_string = '%%prog %s (%s)' % (program_version, program_build_date)

    if argv is None:
        argv = sys.argv[1:]

    # setup argparser
    parser = argparse.ArgumentParser()
    
    parser.add_argument('--version', action='version', version=program_version_string)
    
    # Crash information
    parser.add_argument("--stdout", dest="stdout", help="File containing STDOUT data", metavar="FILE")
    parser.add_argument("--stderr", dest="stderr", help="File containing STDERR data", metavar="FILE")
    parser.add_argument("--crashdata", dest="crashdata", help="File containing external crash data", metavar="FILE")

    # Actions
    parser.add_argument("--refresh", dest="refresh", action='store_true', help="Perform a signature refresh")
    parser.add_argument("--submit", dest="submit", action='store_true', help="Submit a signature to the server")
    parser.add_argument("--search", dest="search", action='store_true', help="Search cached signatures for the given crash")
    parser.add_argument("--generate", dest="generate", action='store_true', help="Create a (temporary) local signature in the cache directory")
    parser.add_argument("--autosubmit", dest="autosubmit", action='store_true', help="Go into auto-submit mode. In this mode, all remaining arguments are interpreted as the crashing command. This tool will automatically obtain GDB crash information and submit it.")
    parser.add_argument("--download", dest="download", type=int, help="Download the testcase for the specified crash entry", metavar="ID")

    # Settings
    parser.add_argument("--sigdir", dest="sigdir", help="Signature cache directory", metavar="DIR")
    parser.add_argument("--serverhost", dest="serverhost", help="Server hostname for remote signature management", metavar="HOST")
    parser.add_argument("--serverport", dest="serverport", type=int, help="Server port to use", metavar="PORT")
    parser.add_argument("--serverproto", dest="serverproto", help="Server protocol to use (default is https)", metavar="PROTO")
    parser.add_argument("--serverauthtokenfile", dest="serverauthtokenfile", help="File containing the server authentication token", metavar="FILE")
    parser.add_argument("--clientid", dest="clientid", help="Client ID to use when submitting issues", metavar="ID")
    parser.add_argument("--platform", dest="platform", help="Platform this crash appeared on", metavar="(x86|x86-64|arm)")
    parser.add_argument("--product", dest="product", help="Product this crash appeared on", metavar="PRODUCT")
    parser.add_argument("--productversion", dest="product_version", help="Product version this crash appeared on", metavar="VERSION")
    parser.add_argument("--os", dest="os", help="OS this crash appeared on", metavar="(windows|linux|macosx|b2g|android)")
    parser.add_argument("--tool", dest="tool", help="Name of the tool that found this issue", metavar="NAME")
    parser.add_argument('--args', dest='args', nargs='+', type=str, help="List of program arguments. Backslashes can be used for escaping and are stripped.")
    parser.add_argument('--env', dest='env', nargs='+', type=str, help="List of environment variables in the form 'KEY=VALUE'")
    parser.add_argument('--metadata', dest='metadata', nargs='+', type=str, help="List of metadata variables in the form 'KEY=VALUE'")
    parser.add_argument("--binary", dest="binary", help="Binary that has a configuration file for reading", metavar="BINARY")


    parser.add_argument("--testcase", dest="testcase", help="File containing testcase", metavar="FILE")
    parser.add_argument("--testcasequality", dest="testcasequality", default="0", help="Integer indicating test case quality (0 is best and default)", metavar="VAL")

    # Options that affect how signatures are generated
    parser.add_argument("--forcecrashaddr", dest="forcecrashaddr", action='store_true', help="Force including the crash address into the signature")
    parser.add_argument("--forcecrashinst", dest="forcecrashinst", action='store_true', help="Force including the crash instruction into the signature (GDB only)")
    parser.add_argument("--numframes", dest="numframes", default=8, type=int, help="How many frames to include into the signature (default is 8)")

    parser.add_argument('rargs', nargs=argparse.REMAINDER)

    if len(argv) == 0:
        parser.print_help()
        return 2

    # process options
    opts = parser.parse_args(argv)
    
    # Check that one action is specified
    actions = [ "refresh", "submit", "search", "generate", "autosubmit", "download" ]
    
    haveAction = False
    for action in actions:
        if getattr(opts, action):
            if haveAction:
                print("Error: Cannot specify multiple actions at the same time", file=sys.stderr)
                return 2
            haveAction = True
    if not haveAction:
        print("Error: Must specify an action", file=sys.stderr)
        return 2
    
    # In autosubmit mode, we try to open a configuration file for the binary specified
    # on the command line. It should contain the binary-specific settings for submitting.
    if opts.autosubmit:
        if not opts.rargs:
            print("Error: Action --autosubmit requires test arguments to be specified", file=sys.stderr)
            return 2
    
        # Store the binary candidate only if --binary wasn't also specified
        if not opts.binary:
            opts.binary = opts.rargs[0]
        
        # We also need to check that (apart from the binary), there is only one file on the command line 
        # (the testcase), if it hasn't been explicitely specified.
        testcase = opts.testcase
        testcaseidx = None
        if testcase == None:
            for idx, arg in enumerate(opts.rargs[1:]):
                if os.path.exists(arg):
                    if testcase:
                        print("Error: Multiple potential testcases specified on command line. Must explicitely specify test using --testcase.")
                        return 2
                    testcase = arg
                    testcaseidx = idx
    
    # Either --autosubmit was specified, or someone specified --binary manually
    # Check that the binary actually exists
    if opts.binary and not os.path.exists(opts.binary):
            print("Error: Specified binary does not exist: %s" % opts.binary)
            return 2
        
    stdout = None
    stderr = None
    crashdata = None
    crashInfo = None
    args = None
    env = None
    metadata = {}
            
    if opts.search or opts.generate or opts.submit or opts.autosubmit:
        if opts.metadata:
            metadata.update(dict(kv.split('=', 1) for kv in opts.metadata))
        
        if opts.autosubmit:
            # Try to automatically get arguments from the command line
            # If the testcase is not the last argument, leave it in the
            # command line arguments and replace it with a generic placeholder.
            if testcaseidx == len(opts.rargs[1:]) - 1:
                args = opts.rargs[1:-1]
            else:
                args = opts.rargs[1:]
                if testcaseidx != None:
                    args[testcaseidx] = "TESTFILE"
        else:
            if opts.args:
                args = [arg.replace('\\', '') for arg in opts.args]
            
        if opts.env:
            env = dict(kv.split('=', 1) for kv in opts.env)
            
        # Start without any ProgramConfiguration
        configuration = None
        
        # If we have a binary, try using that to create our ProgramConfiguration
        if opts.binary:
            configuration = ProgramConfiguration.fromBinary(opts.binary)
            if configuration:
                if env:
                    configuration.addEnvironmentVariables(env)
                if args:
                    configuration.addProgramArguments(args)
                if metadata:
                    configuration.addMetadata(metadata)
        
        # If configuring through binary failed, try to manually create ProgramConfiguration from command line arguments
        if configuration == None:
            if opts.platform == None or opts.product == None or opts.os == None:
                print("Error: Must specify/configure at least --platform, --product and --os", file=sys.stderr)
                return 2
            
            configuration = ProgramConfiguration(opts.product, opts.platform, opts.os, opts.product_version, env, args, metadata)

        
        if not opts.autosubmit:
            if opts.stderr == None and opts.crashdata == None:
                print("Error: Must specify at least either --stderr or --crashdata file", file=sys.stderr)
                return 2
            
            if opts.stdout:
                with open(opts.stdout) as f:
                    stdout = f.read()
            
            if opts.stderr:
                with open(opts.stderr) as f:
                    stderr = f.read()
                
            if opts.crashdata:
                with open(opts.crashdata) as f:
                    crashdata = f.read()

            crashInfo = CrashInfo.fromRawCrashData(stdout, stderr, configuration, auxCrashData=crashdata)
            if opts.testcase:
                (testCaseData, isBinary) = Collector.read_testcase(opts.testcase)
                if not isBinary:
                    crashInfo.testcase = testCaseData
                
    serverauthtoken = None
    if opts.serverauthtokenfile:
        with open(opts.serverauthtokenfile) as f:
            serverauthtoken = f.read().rstrip()

    collector = Collector(opts.sigdir, opts.serverhost, opts.serverport, opts.serverproto, serverauthtoken, opts.clientid, opts.tool)
    
    if opts.refresh:
        collector.refresh()
        return 0
        
    if opts.submit:
        testcase = opts.testcase        
        collector.submit(crashInfo, testcase, opts.testcasequality, metadata)
        return 0
    
    if opts.search:
        (sig, metadata) = collector.search(crashInfo)
        if sig == None:
            print("No match found")
            return 3
        print(sig)
        if metadata:
            print(json.dumps(metadata, indent=4))
        return 0
    
    if opts.generate:
        sigFile = collector.generate(crashInfo, opts.forcecrashaddr, opts.forcecrashinst, opts.numframes)
        if not sigFile:
            print("Failed to generate a signature for the given crash information.", file=sys.stderr)
            return 2
        print(sigFile)
        return 0
    
    if opts.autosubmit:
        runner = AutoRunner.fromBinaryArgs(opts.rargs[0], opts.rargs[1:])
        if runner.run():
            crashInfo = runner.getCrashInfo(configuration)
            collector.submit(crashInfo, testcase, opts.testcasequality, metadata)
        else:
            print("Error: Failed to reproduce the given crash, cannot submit.", file=sys.stderr)
            return 2

    if opts.download:
        retFile = collector.download(opts.download)
        if not retFile:
            print("Specified crash entry does not have a testcase", file=sys.stderr)
            return 2
        print(retFile)
        return 0

if __name__ == "__main__":
    sys.exit(main())
