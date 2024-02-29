import csv
import argparse
import json
import ssl
import re
from string import Template
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from datetime import datetime

# Constants
CSV_HEADER = [
    "Cluster Name",
    "Environment",
    "Cluster Descriptor",
    "Node Name",
    "CVE",
    "Fixable",
    "Severity",
    "CVSS",
    "Env. Impact",
    "Impact Score",
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
    "id": "${nodeId}",
    "query": "",
    "policyQuery": "Category:Vulnerability Management",
    "scopeQuery": "CLUSTER ID: ${clusterId}",
    "pagination": {
      "offset": 0,
      "limit": 0,
      "sortOption": {
        "field": "CVSS",
        "reversed": true
      }
    }
  },
  "query": "query ${operationName}($$id: ID!, $$pagination: Pagination, $$query: String, $$policyQuery: String, $$scopeQuery: String) {\n  result: node(id: $$id) {\n    id\n    nodeVulnerabilityCount(query: $$query)\n    nodeVulnerabilities(query: $$query, pagination: $$pagination) {\n      ...nodeCVEFields\n      __typename\n    }\n    unusedVarSink(query: $$policyQuery)\n    unusedVarSink(query: $$scopeQuery)\n    __typename\n  }\n}\n\nfragment nodeCVEFields on NodeVulnerability {\n  createdAt\n  cve\n  cvss\n  envImpact\n  fixedByVersion\n  id\n  impactScore\n  isFixable(query: $$scopeQuery)\n  lastModified\n  lastScanned\n  link\n  publishedOn\n  scoreVersion\n  severity\n  summary\n  suppressActivation\n  suppressExpiry\n  suppressed\n  componentCount: nodeComponentCount\n  nodeCount\n  operatingSystem\n  __typename\n}\n"
}""")

# Prepare for API calls
rhacsCentralUrl = None
rhacsApiToken = None
csvFileName = None
authorizationHeader = None
requestContext = ssl.create_default_context()
requestContext.check_hostname = False
requestContext.verify_mode = ssl.CERT_NONE


# Main function
def main():
    # We will modify these global variables
    global rhacsCentralUrl, rhacsApiToken, csvFileName, apiHeader
    
    # Initialize arguments parser
    parser = argparse.ArgumentParser()

    parser.add_argument("-u", "--url", help="RHACS CENTRAL URL, e.g. https://central-stackrox.apps.myocpcluster.com", required=True)
    parser.add_argument("-t", "--token", help="RHACS API token", required=True)
    parser.add_argument("-o", "--output", help="Output CSV file name", required=True)
    arguments = parser.parse_args()
    
    rhacsCentralUrl = arguments.url
    rhacsApiToken = arguments.token
    csvFileName = arguments.output

    # Prepare for API calls
    apiHeader = {
        "Authorization": "Bearer " + rhacsApiToken,
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": 0,
        "Accept": "application/json"
    }

    responseJson = getJsonFromRhacsApi("/clusters")
    if responseJson is not None:
        # Create the CSV file
        with open(csvFileName, "w", newline="") as f:
            writer = csv.writer(f, dialect="excel")
            writer.writerow(CSV_HEADER)

            # Process all clusters
            clusters = responseJson["clusters"]
            for cluster in clusters:
                clusterId = cluster["id"]
                clusterName = cluster["name"]
                clusterEnvironment = ""
                clusterDescriptor = ""

                print(f"Inspecting nodes in cluster:{clusterName}...")

                # Try to parse cluster info
                try:
                    regexResult = re.search(CLUSTER_INFO_REGEX, clusterName)
                    if regexResult.group(1) is not None:
                        clusterName = regexResult.group(1)
                    if regexResult.group(2) is not None:
                        clusterEnvironment = regexResult.group(2)
                    if regexResult.group(3) is not None:
                        clusterDescriptor = regexResult.group(3)
                except:
                    pass

                # Process all nodes in this cluster
                responseJson = getJsonFromRhacsApi("/nodes/" + clusterId)
                nodes = responseJson["nodes"]
                nodeCount = len(nodes)
                currentNodeIndex = 0
                for node in nodes:
                    nodeId = node["id"]
                    nodeName = node["name"]
 
                    currentNodeIndex += 1
                    print(f"{currentNodeIndex} of {nodeCount} - Inspecting {clusterName}/{nodeName}...")

                    try:
                        operationName = "getNodeNODE_CVE"
                        graphQlRequest = GRAPHQL_REQUEST_TEMPLATE.substitute(
                            operationName = operationName,
                            clusterId = clusterId,
                            nodeId = nodeId
                        ).replace("\n", "")

                        responseJson = getJsonFromRhacsGraphQl(operationName, graphQlRequest)
                        if responseJson is not None:
                            cves = responseJson["data"]["result"]["nodeVulnerabilities"]

                            for cve in cves:
                                # Parse the severity
                                severity = ""
                                try:
                                    severity = VULNERABILITY_SEVERITY[cve["severity"]]
                                except:
                                    pass

                                # Write the cve details
                                writer.writerow([
                                    clusterName,
                                    clusterEnvironment,
                                    clusterDescriptor,
                                    nodeName,
                                    cve["cve"],
                                    "Fixable" if cve["isFixable"] else "Not Fixable",
                                    severity,
                                    "{0:.1f}".format(cve["cvss"]),
                                    "{0:.0f}%".format(cve["envImpact"]*100),
                                    "{0:.2f}".format(cve["impactScore"]),
                                    cve["publishedOn"],
                                    cve["createdAt"],
                                    cve["link"],
                                    cve["summary"]
                                ])
                                f.flush()

                    except Exception as ex:
                        print(f"Not completing {clusterName}/{nodeName} due to ERROR:{type(ex)=}:{ex=}.")

        print(f"Successfully generated {csvFileName}\n")
                    
def getJsonFromRhacsApi(requestPath):
    url=rhacsCentralUrl + "/v1" + requestPath
    with urlopen(Request(
        url=url,
        headers=apiHeader),
        context=requestContext) as response:
        if response.status != 200:
            print(f"Error: {response.status} - {response.msg} for request:{url}")
            return None
        else:
            return json.loads(response.read())
        
def getJsonFromRhacsGraphQl(operationName, graphQlRequest):
    url = rhacsCentralUrl + "/api/graphql"
    params = {"opname": operationName}
    url_with_params = f"{url}?{urlencode(params)}"
    jsonBody = graphQlRequest.encode('utf-8')
    apiHeader["Content-Length"] = len(jsonBody)

    with urlopen(Request(
        url=url_with_params,
        method="POST",
        headers=apiHeader),
        data=jsonBody,
        context=requestContext) as response:
        if response.status != 200:
            print(f"Error: {response.status} - {response.msg} for request:{url}")
            return None
        else:
            return json.loads(response.read())

if __name__=="__main__": 
    main() 