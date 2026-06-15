import yaml
import json
import os
import sys
import hashlib
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import re
import time

STATE_FILE = "state.json"

class StateManager:
    def __init__(self):
        self.state = {}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    self.state = json.load(f)
            except:
                self.state = {}

    def is_new(self, company, job_id=None, content_hash=None):
        if company not in self.state:
            self.state[company] = {"jobs": [], "hash": ""}
        
        if job_id:
            if job_id not in self.state[company]["jobs"]:
                self.state[company]["jobs"].append(job_id)
                return True
            return False
        elif content_hash:
            if content_hash != self.state[company].get("hash"):
                self.state[company]["hash"] = content_hash
                return True
            return False

    def save(self):
        with open(STATE_FILE, 'w') as f:
            json.dump(self.state, f, indent=2)

class Notifier:
    def __init__(self, config):
        self.token = os.environ.get("TELEGRAM_TOKEN", config.get("telegram", {}).get("token", ""))
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", config.get("telegram", {}).get("chat_id", ""))

    def send(self, message):
        print(f"ALERT: {message.replace('<b>', '').replace('</b>', '').replace('<a href=', '').replace('</a>', '')}")
        if self.token and self.chat_id and not self.token.startswith("${"):
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
            try:
                requests.post(url, json=payload)
            except Exception as e:
                print(f"Failed to send telegram message: {e}")

class ATSHunter:
    def __init__(self, config):
        self.keywords = [k.lower() for k in config.get("keywords", [])]
        self.locations = [l.lower() for l in config.get("locations", [])]

    def matches_criteria(self, title, location):
        if not title: return False
        title_lower = title.lower()
        location_lower = location.lower() if location else ""
        
        keyword_match = any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in self.keywords) if self.keywords else True
        location_match = any(l in location_lower for l in self.locations) if self.locations else True
        
        return keyword_match and location_match

    def hunt_ashby(self, slug):
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200: return []
            jobs = res.json().get("jobs", [])
            matches = []
            for job in jobs:
                title = job.get("title", "")
                location = job.get("location", "")
                if self.matches_criteria(title, location):
                    matches.append({"id": job.get("id"), "title": title, "location": location, "url": job.get("jobUrl")})
            return matches
        except Exception as e:
            print(f"Ashby error for {slug}: {e}")
            return []

    def hunt_lever(self, slug):
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200: return []
            jobs = res.json()
            matches = []
            for job in jobs:
                title = job.get("text", "")
                location = job.get("categories", {}).get("location", "")
                if self.matches_criteria(title, location):
                    matches.append({"id": job.get("id"), "title": title, "location": location, "url": job.get("hostedUrl")})
            return matches
        except Exception as e:
            print(f"Lever error for {slug}: {e}")
            return []

    def hunt_greenhouse(self, slug):
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200: return []
            jobs = res.json().get("jobs", [])
            matches = []
            for job in jobs:
                title = job.get("title", "")
                location = job.get("location", {}).get("name", "")
                if self.matches_criteria(title, location):
                    matches.append({"id": str(job.get("id")), "title": title, "location": location, "url": job.get("absolute_url")})
            return matches
        except Exception as e:
            print(f"Greenhouse error for {slug}: {e}")
            return []

    def hunt_oracle_hcm(self, site_number, keyword="intern", location_id=None):
        """Hunter for Oracle HCM Cloud portals (used by JP Morgan, etc.)"""
        url = "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        params = f"onlyData=true&expand=requisitionList.secondaryLocations,requisitionList.requisitionFlexFields&finder=findReqs;siteNumber={site_number},keyword={keyword}"
        if location_id:
            params += f",locationId={location_id}"
        params += "&limit=25"
        try:
            res = requests.get(f"{url}?{params}", timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if res.status_code != 200: return []
            data = res.json()
            items = data.get("items", [])
            if not items: return []
            reqs = items[0].get("requisitionList", [])
            matches = []
            for job in reqs:
                title = job.get("Title", "")
                location = job.get("PrimaryLocation", "")
                if self.matches_criteria(title, location):
                    job_id = str(job.get("Id", ""))
                    job_url = f"https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/{site_number}/job/{job_id}"
                    matches.append({"id": job_id, "title": title, "location": location, "url": job_url})
            return matches
        except Exception as e:
            print(f"Oracle HCM error: {e}")
            return []

class CustomWebHunter:
    def __init__(self, config):
        self.keywords = [k.lower() for k in config.get("keywords", [])]
        self.locations = [l.lower() for l in config.get("locations", [])]

    def hunt(self, page_config):
        url = page_config["url"]
        wait_for = page_config.get("wait_for_selector")
        css_selector = page_config.get("css_selector")
        keyword_filter = page_config.get("keyword_filter", False)
        location_filter = page_config.get("location_filter", True) # Default to true for safety
        
        try:
            with sync_playwright() as p:
                # Use Chromium for JS rendering
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                
                if wait_for:
                    try:
                        page.wait_for_selector(wait_for, timeout=10000)
                    except:
                        print(f"Timeout waiting for selector {wait_for} on {url}")
                
                # Give JS frameworks time to render dynamic content
                time.sleep(3)
                
                content = page.content()
                browser.close()
                
            soup = BeautifulSoup(content, "html.parser")
            
            if css_selector:
                elements = soup.select(css_selector)
                if not elements:
                    text_content = ""
                else:
                    text_content = " ".join([el.get_text(separator=' ', strip=True) for el in elements])
            else:
                # Fallback to whole body text
                text_content = soup.body.get_text(separator=' ', strip=True) if soup.body else ""
            
            # Create a hash of the text content to detect visual changes
            content_hash = hashlib.md5(text_content.encode('utf-8')).hexdigest()
            
            matches_keywords = False
            matches_locations = False
            
            text_lower = text_content.lower()
            
            if keyword_filter and self.keywords:
                matches_keywords = any(re.search(r'\b' + re.escape(k) + r'\b', text_lower) for k in self.keywords)
            else:
                matches_keywords = True # Ignore keywords if filter is false
                
            if location_filter and self.locations:
                matches_locations = any(l in text_lower for l in self.locations)
            else:
                matches_locations = True
                
            is_match = matches_keywords and matches_locations
                
            return {
                "hash": content_hash,
                "matches_keywords": matches_keywords,
                "matches_locations": matches_locations,
                "is_match": is_match,
            }
        except Exception as e:
            print(f"Custom Web error for {url}: {e}")
            return None

def main():
    test_mode = "--test" in sys.argv
    
    if not os.path.exists("config.yaml"):
        print("config.yaml not found! Please create one based on the template.")
        return

    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    state_manager = StateManager()
    notifier = Notifier(config)
    ats_hunter = ATSHunter(config)
    custom_hunter = CustomWebHunter(config)

    if test_mode:
        print("="*60)
        print("🧪 TEST MODE — No state changes, no notifications")
        print(f"Keywords: {config.get('keywords', [])}")
        print(f"Locations: {config.get('locations', [])}")
        print("="*60)

    # 1. Process ATS Companies
    print("\n📡 Hunting ATS boards...")
    for comp in config.get("ats_companies", []):
        name = comp["name"]
        ats_type = comp["ats"]
        slug = comp.get("slug", "")
        
        matches = []
        if ats_type == "ashby":
            matches = ats_hunter.hunt_ashby(slug)
        elif ats_type == "lever":
            matches = ats_hunter.hunt_lever(slug)
        elif ats_type == "greenhouse":
            matches = ats_hunter.hunt_greenhouse(slug)
        elif ats_type == "oracle_hcm":
            matches = ats_hunter.hunt_oracle_hcm(
                site_number=comp.get("site_number", "CX_1001"),
                keyword=comp.get("keyword", "intern"),
                location_id=comp.get("location_id")
            )
        
        if test_mode:
            print(f"  {'✅' if matches else '—'} {name} ({ats_type}): {len(matches)} matches")
            for m in matches[:3]:
                print(f"      → {m['title']} | {m['location']}")
            if len(matches) > 3:
                print(f"      ... and {len(matches) - 3} more")
        else:
            for match in matches:
                if state_manager.is_new(name, job_id=match["id"]):
                    msg = f"🚀 <b>New Role at {name}</b>\n\n<b>Title:</b> {match['title']}\n<b>Location:</b> {match['location']}\n<a href='{match['url']}'>Apply Here</a>"
                    notifier.send(msg)

    # 2. Process Custom Web Pages
    print("\n🌐 Hunting Custom Web Pages (Playwright JS-rendered)...")
    for page_config in config.get("custom_pages", []):
        name = page_config["name"]
        url = page_config["url"]
        
        print(f"  Checking {name}...")
        result = custom_hunter.hunt(page_config)
        
        if test_mode:
            if result:
                status = "✅ MATCH" if result["is_match"] else "— NO match"
                details = f"(kw={result['matches_keywords']}, loc={result['matches_locations']})"
                print(f"  {status} {details} | hash={result['hash'][:12]}...")
            else:
                print(f"  ❌ FAILED to load page")
        else:
            if result:
                if result["is_match"]:
                    if state_manager.is_new(name, content_hash=result["hash"]):
                        msg = f"🔍 <b>Changes detected at {name}</b>\n\nKeywords & Locations found in new page update! Check the page:\n<a href='{url}'>{url}</a>"
                        notifier.send(msg)
                else:
                    # Save the hash anyway so we don't trigger when keywords/locations don't match,
                    # but we want to store the baseline state.
                    state_manager.is_new(name, content_hash=result["hash"])

    if not test_mode:
        state_manager.save()
    
    print("\n✅ Done!")

if __name__ == "__main__":
    main()
