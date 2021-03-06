'''
Tests

@author:     Christian Holler (:decoder)

@license:

This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.

@contact:    choller@mozilla.com
'''

import unittest
import requests
from requests.exceptions import ConnectionError

# Server and credentials (user/password) used for testing
testServerURL = "http://127.0.0.1:8000/rest/"
testAuthCreds = ("admin", "admin")

# Check if we have a remote server for testing, if not, skip tests
haveServer = True
try:
    requests.get(testServerURL)
except ConnectionError, e:
    haveServer = False

@unittest.skipIf(not haveServer, reason="No remote server available for testing")
class TestRESTCrashEntryInterface(unittest.TestCase):
    def runTest(self):
        url = testServerURL + "crashes/"
        
        # Must yield forbidden without authentication
        self.assertEqual(requests.get(url).status_code, requests.codes["forbidden"])
        self.assertEqual(requests.post(url, {}).status_code, requests.codes["forbidden"])
        self.assertEqual(requests.put(url, {}).status_code, requests.codes["forbidden"])
        
        # Retry with authentication
        response = requests.get(url, auth=testAuthCreds)

        # Must be empty now
        self.assertEqual(response.status_code, requests.codes["ok"])
        lengthBeforePost = len(response.json())
        #self.assertEqual(response.json(), [])
        
        data = {
                "rawStdout" : "data on\nstdout", 
                "rawStderr" : "data on\nstderr",
                "rawCrashData" : "some\ncrash\ndata",
                "testcase" : "foo();\ntest();",
                "platform" : "x86",
                "product" : "mozilla-central",
                "product_version" : "ba0bc4f26681",
                "os" : "linux",
                "client" : "client1",
                }
        
        self.assertEqual(requests.post(url, data, auth=testAuthCreds).status_code, requests.codes["created"])
        response = requests.get(url, auth=testAuthCreds)
        
        json = response.json()
        self.assertEqual(len(json), lengthBeforePost + 1)
        self.assertEqual(json[lengthBeforePost]["product_version"], "ba0bc4f26681")
        
@unittest.skipIf(not haveServer, reason="No remote server available for testing")
class TestRESTSignatureInterface(unittest.TestCase):
    def runTest(self):
        url = testServerURL + "signatures/"
        
        # Must yield forbidden without authentication
        self.assertEqual(requests.get(url).status_code, requests.codes["forbidden"])
        self.assertEqual(requests.post(url, {}).status_code, requests.codes["forbidden"])
        self.assertEqual(requests.put(url, {}).status_code, requests.codes["forbidden"])
        
        # Retry with authentication
        response = requests.get(url,  auth=testAuthCreds)

        self.assertEqual(response.json(), [])


if __name__ == "__main__":
    unittest.main()