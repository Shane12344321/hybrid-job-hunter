import requests
import json
import time
import re
from concurrent.futures import ThreadPoolExecutor

companies = [
    "Google", "Microsoft", "Amazon", "Apple", "Uber", "LinkedIn", "Atlassian", "Salesforce",
    "Adobe", "Intuit", "Oracle", "Cisco", "IBM", "SAP", "Red Hat", "Yahoo", "Rakuten",
    "Gojek", "Grab", "Expedia",
    "NVIDIA", "Intel", "AMD", "Qualcomm", "ARM", "Texas Instruments", "Broadcom", "Samsung",
    "Western Digital", "Micron", "Applied Materials", "NXP", "Synopsys", "Cadence",
    "MediaTek", "Ericsson", "Nokia", "Analog Devices", "Marvell", "Silicon Labs",
    "DE Shaw", "Tower Research", "AlphaGrep", "WorldQuant", "Trexquant", "Arcesium",
    "Goldman Sachs", "Morgan Stanley", "JPMorgan", "Bloomberg", "PayPal", "Visa",
    "Mastercard", "American Express", "BNY Mellon", "Barclays", "Fidelity", "BlackRock",
    "Wells Fargo", "Stripe",
    "Snowflake", "Databricks", "MongoDB", "Confluent", "Rubrik", "Nutanix", "ServiceNow",
    "Twilio", "Zoom", "Dropbox", "CrowdStrike", "Palo Alto Networks", "Arista Networks",
    "Juniper Networks", "VMware", "Shopify", "GitHub", "Cloudflare", "Akamai", "Zscaler",
    "Postman", "BrowserStack", "Razorpay", "Swiggy", "Zomato", "Flipkart", "PhonePe",
    "Cred", "Zepto", "Directi", "Thoughtworks", "Eightfold", "Glean", "Rippling",
    "Sprinklr", "Dream11", "MindTickle", "Groww", "Zerodha", "Innovaccer"
]

def clean_slug(name):
    # remove special chars and spaces
    s = name.lower()
    s = re.sub(r'[^a-z0-9]', '', s)
    return s

def check_greenhouse(slug):
    try:
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=5)
        if r.status_code == 200:
            return "greenhouse", slug
    except: pass
    return None

def check_lever(slug):
    try:
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=5)
        if r.status_code == 200 and len(r.json()) > 0:
            return "lever", slug
    except: pass
    return None

def check_ashby(slug):
    try:
        r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=5)
        if r.status_code == 200:
            return "ashby", slug
    except: pass
    return None

def check_company(name):
    base_slug = clean_slug(name)
    slugs_to_try = [base_slug]
    if name == "Tower Research": slugs_to_try.append("towerresearchcapital")
    if name == "Palo Alto Networks": slugs_to_try.append("paloaltonetworks")
    if name == "Arista Networks": slugs_to_try.append("aristanetworks")
    
    for slug in slugs_to_try:
        for check in [check_greenhouse, check_lever, check_ashby]:
            res = check(slug)
            if res:
                return name, res[0], res[1]
    return name, None, None

results = []
with ThreadPoolExecutor(max_workers=20) as executor:
    for res in executor.map(check_company, companies):
        if res[1]:
            print(f"  - name: {res[0]}\\n    ats: {res[1]}\\n    slug: {res[2]}")

