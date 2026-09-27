import os
import requests
from requests.auth import HTTPBasicAuth

def test_read_jira_ticket():
    # 1. Setup Environment Configuration
    domain = "your-company-test.atlassian.net"
    issue_key = "TEST-1"  # Replace with a real ticket key in your dev instance
    
    email = os.getenv("JIRA_USER_EMAIL")
    api_token = os.getenv("JIRA_API_TOKEN")
    
    url = f"https://{domain}/rest/api/3/issue/{issue_key}"
    auth = HTTPBasicAuth(email, api_token)
    headers = {"Accept": "application/json"}

    # 2. Execute Request
    response = requests.request("GET", url, headers=headers, auth=auth)

    # 3. Assertions for Integration Test
    assert response.status_code == 200, f"Failed to fetch ticket. Status: {response.status_code}"
    
    data = response.json()
    assert data["key"] == issue_key
    assert "fields" in data
    print(f"Successfully read ticket title: {data['fields']['summary']}")
