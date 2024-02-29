import csv
import argparse
import json
import ssl
import re
from string import Template
from urllib.request import urlopen, Request
from datetime import datetime

# Constants
UBI_IDENTIFIER_IN_LABEL = "\"url\"=\"https://access.redhat.com/containers/#/registry.access.redhat.com/ubi"
OS_ID_RHEL = "rhel"
UBI_PREFIX = "ubi"
UBI_REGEX = r"(?:https\://.+/([^/]+)/images/(\d[\d.]*)\-(\d[\d.]*))"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S %z"
CSV_HEADER = [
    "Cluster Name",
    "Environment",
    "Cluster Descriptor",
    "CVE",
    "Fixable",
    "Severity",
    "CVSS",
    "Impact Score",
    "Namespaces",
    "Images",
    "Published On",
    "Discovered On",
    "Link",
    "Summary"
]
VULNERABILITY_SEVERITY = {
    "CRITICAL_VULNERABILITY_SEVERITY": "Critical",
    "IMPORTANT_VULNERABILITY_SEVERITY": "Important",
    "MODERATE_VULNERABILITY_SEVERITY": "Moderate",
    "LOW_VULNERABILITY_SEVERITY": "Low",
    "UNKNOWN_VULNERABILITY_SEVERITY": "Unknown"
}
IS_INCLUDE_OPENSHIFT_NAMESPACE = False

# The cluster regex matches the following:
# ocps4 - uat_abc_def123 <-- cluster name: ocps4, environment: uat, cluster descriptor: abc_def123
# ocps4 - uat            <-- cluster name: ocps4, environment: uat
# ocps4                  <-- cluster name: ocps4
CLUSTER_INFO_REGEX = r"([^\W_]+)(?:[\W_]+([^\W_]+)(?:[\W_]+(.*))?)?$"

# GraphQL request payload
GRAPHQL_REQUEST_TEMPLATE = Template("""
{
  "operationName": "${operationName}",
  "variables": {
    "id": "${clusterId}",
    "query": "",
    "policyQuery": "Category:Vulnerability Management",
    "scopeQuery": "CLUSTER ID:${clusterId}",
    "pagination": {
      "offset": 0,
      "limit": 0,
      "sortOption": {
        "field": "CVSS",
        "reversed": true
      }
    }
  },
  "query": "query ${operationName}($$id: ID!, $$pagination: Pagination, $$query: String, $$policyQuery: String, $$scopeQuery: String) {\n  result: cluster(id: $$id) {\n    id\n    imageVulnerabilityCount(query: $$query)\n    imageVulnerabilities(query: $$query, pagination: $$pagination) {\n      ...imageCVEFields\n      __typename\n    }\n    unusedVarSink(query: $$policyQuery)\n    unusedVarSink(query: $$scopeQuery)\n    __typename\n  }\n}\n\nfragment imageCVEFields on ImageVulnerability {\n  createdAt\n  cve\n  cvss\n  discoveredAtImage\n  envImpact\n  fixedByVersion\n  id\n  impactScore\n  isFixable(query: $$scopeQuery)\n  lastModified\n  lastScanned\n  link\n  operatingSystem\n  publishedOn\n  scoreVersion\n  severity\n  summary\n  suppressActivation\n  suppressExpiry\n  suppressed\n  vulnerabilityState\n  componentCount: imageComponentCount\n  imageCount\n  deploymentCount\n  __typename\n}\n"
}""")

# Prepare for API calls
rhacsCentralUrl = None
rhacsApiToken = None
csvFileName = None
authorizationHeader = None
requestContext = ssl.create_default_context()
requestContext.check_hostname = False
requestContext.verify_mode = ssl.CERT_NONE

class ClusterDetail:
    def __init__(self) -> None:
        self.clusterId = ""
        self.clusterName = ""
        self.clusterEnvironment = ""
        self.clusterDescriptor = ""
        self.cveDetails = {}
    
class CveDetail:
    def __init__(self) -> None:
        self.cve = {}
        self.namespaces = []
        self.images = []

# Main function
def main():
    # We will modify these global variables
    global rhacsCentralUrl, rhacsApiToken, csvFileName, authorizationHeader
    
    # Initialize arguments parser
    parser = argparse.ArgumentParser()

    parser.add_argument("-u", "--url", help="RHACS Central URL, e.g. https://central-stackrox.apps.myocpcluster.com", required=True)
    parser.add_argument("-t", "--token", help="RHACS API token", required=True)
    parser.add_argument("-o", "--output", help="Output CSV file name", required=True)
    arguments = parser.parse_args()
    
    rhacsCentralUrl = arguments.url
    rhacsApiToken = arguments.token
    csvFileName = arguments.output

    # Prepare for API calls
    authorizationHeader = {
        "Authorization": "Bearer " + rhacsApiToken,
        "Accept": "application/json"
    }

    # This will contain the list of CVEs
    # Grouped by cluster, so some CVEs may appear in multiple clusters
    cvesByCluster = {}

    responseJson = getJsonFromRhacsApi("/deployments")
    if responseJson is not None:
        # Process all deployments across all clusters
        deployments = responseJson["deployments"]

        # Skip all openshift namespaces unless IS_INCLUDE_OPENSHIFT_NAMESPACE is True
        deploymentsToBeInspected = deployments if IS_INCLUDE_OPENSHIFT_NAMESPACE else [deployment for deployment in deployments if not deployment["namespace"].startswith("openshift")] 

        deploymentCount = len(deploymentsToBeInspected)
        currentDeploymentIndex = 0
        for deployment in deploymentsToBeInspected:
            clusterId = deployment["clusterId"]
            clusterNameRaw = deployment["cluster"]
            clusterEnvironment = ""
            clusterDescriptor = ""

            # Try to parse cluster info
            try:
                regexResult = re.search(CLUSTER_INFO_REGEX, clusterNameRaw)
                if regexResult.group(1) is not None:
                    clusterName = regexResult.group(1)
                if regexResult.group(2) is not None:
                    clusterEnvironment = regexResult.group(2)
                if regexResult.group(3) is not None:
                    clusterDescriptor = regexResult.group(3)
            except:
                pass

            # Initialise the CVE list object if it has not been initialised
            if clusterId not in cvesByCluster:
                cvesByCluster[clusterId] = ClusterDetail()
                cvesByCluster[clusterId].clusterId = clusterId
                cvesByCluster[clusterId].clusterName = clusterName
                cvesByCluster[clusterId].clusterEnvironment = clusterEnvironment
                cvesByCluster[clusterId].clusterDescriptor = clusterDescriptor
            
            currentClusterDetail = cvesByCluster[clusterId]

            namespace = deployment["namespace"]
            deploymentId = deployment["id"]
            deploymentName = deployment["name"]

            # Get the deployment detail
            currentDeploymentIndex += 1
            print(f"{currentDeploymentIndex} of {deploymentCount} - Inspecting {clusterName}/{namespace}/{deploymentName}...")

            try:
                responseJson = getJsonFromRhacsApi("/deployments/" + deploymentId)
                if responseJson is not None:
                    containers = responseJson["containers"]
                    for container in containers:
                        image = container["image"]
                        imageId = image["id"]
                        imageFullName = image["name"]["fullName"]
                        
                        # Get the image detail
                        createdOn = ""
                        os = ""
                        ubiName = ""
                        ubiVersion = ""
                        ubiRelease = ""
                        try:
                            responseJson = getJsonFromRhacsApi("/images/" + imageId)
                            if responseJson is not None:
                                metadataJson = responseJson["metadata"]["v1"]
                                createdOn = metadataJson["created"]
                                os = responseJson["scan"]["operatingSystem"]

                                # Get more details if it's a rhel-based image
                                if os.startswith(OS_ID_RHEL):
                                    for layer in metadataJson["layers"]:
                                        if layer["instruction"] == "LABEL":
                                            value = layer["value"]
                                            # UBI-specific metadata checking
                                            if UBI_IDENTIFIER_IN_LABEL in value:
                                                regexResult = re.search(UBI_REGEX, value)
                                                if regexResult is not None:
                                                    ubiName = regexResult.group(1)
                                                    ubiVersion = regexResult.group(2)
                                                    ubiRelease = regexResult.group(3)
                                                    # Exit the current loop
                                                    break

                                    # If the base image labels have been removed,
                                    # try to get the metadata from the url
                                    if ubiName == "":
                                        labels = metadataJson["labels"]
                                        if hasattr(labels, "url"):
                                            regexResult = re.search(UBI_REGEX, labels["url"])
                                            if regexResult is not None:
                                                ubiName = regexResult.group(1)
                                                ubiVersion = regexResult.group(2)
                                                ubiRelease = regexResult.group(3)
                                        
                                        # If that failed, try to get the metadata from the labels
                                        if ubiName == "":
                                            ubiName = labels["name"]
                                            ubiVersion = labels["version"]
                                            ubiRelease = labels["release"]

                                    # Ignore non-ubi metadata
                                    if not ubiName.startswith(UBI_PREFIX):
                                        ubiName = ""
                                        ubiVersion = ""
                                        ubiRelease = ""

                        except Exception as ex:
                            os = type(ex)
                            ubiName = ex
                            print(f"Image:{imageFullName} has the following ERROR:{type(ex)=}:{ex=}.")

                        finally:
                            # Get the list of CVEs
                            for component in responseJson["scan"]["components"]:
                                for cve in component["vulns"]:
                                    # Add to the list of CVEs if it has not been added
                                    cveId = cve["cve"]
                                    if cveId not in currentClusterDetail.cveDetails:
                                        currentClusterDetail.cveDetails[cveId] = CveDetail()

                                    currentCveDetail = currentClusterDetail.cveDetails[cveId]
                                    
                                    currentCveDetail.cve = cve
                                    if namespace not in currentCveDetail.namespaces:
                                        currentCveDetail.namespaces.append(namespace)
                                    if imageFullName not in currentCveDetail.images:
                                        currentCveDetail.images.append(imageFullName)

            except Exception as ex:
                print(f"Not completing {clusterName}/{namespace}/{deploymentName} due to ERROR:{type(ex)=}:{ex=}.")

    # Create the CSV file
    with open(csvFileName, "w", newline="") as f:
        writer = csv.writer(f, dialect="excel")
        writer.writerow(CSV_HEADER)

        # Sort the CVEs by environment, cluster name, and CVSS score
        sortedByClusterEnvironmentAndName = sorted(cvesByCluster,  key = lambda clusterId : (cvesByCluster[clusterId].clusterEnvironment, cvesByCluster[clusterId].clusterName))
        for clusterId in sortedByClusterEnvironmentAndName:
            clusterDetail = cvesByCluster[clusterId]

            for cveId in clusterDetail.cveDetails:
                cveDetail = clusterDetail.cveDetails[cveId]
                cveData = cveDetail.cve

                # Parse the severity
                severity = ""
                try:
                    severity = VULNERABILITY_SEVERITY[cveData["severity"]]
                except:
                    pass

                writer.writerow([
                    clusterDetail.clusterName,
                    clusterDetail.clusterEnvironment,
                    clusterDetail.clusterDescriptor,
                    cveId,
                    "Fixable" if "fixedBy" in cveData else "Not Fixable",
                    severity,
                    "{0:.1f}".format(cveData["cvss"]),
                    "{0:.2f}".format(cveData["cvssV3"]["impactScore"]) if cveData["cvssV3"] is not None else "0.00",
                    "\n".join(cveDetail.namespaces),
                    "\n".join(cveDetail.images),
                    cveData["publishedOn"] if cveData["publishedOn"] is not None else "",
                    cveData["firstSystemOccurrence"],
                    cveData["link"],
                    cveData["summary"]
                ])
                f.flush()

    print(f"Successfully generated {csvFileName}\n")
                    
def getJsonFromRhacsApi(requestPath):
    url=rhacsCentralUrl + "/v1" + requestPath
    with urlopen(Request(
        url=url,
        headers=authorizationHeader),
        context=requestContext) as response:
        if response.status != 200:
            print(f"Error: {response.status} - {response.msg} for request:{url}")
            return None
        else:
            return json.loads(response.read())
        
if __name__=="__main__": 
    main() 