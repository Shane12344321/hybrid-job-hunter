import yaml

with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)

existing = {c['name'] for c in config.get('custom_pages', [])}

new_companies = [
    {
        "name": "Microsoft",
        "url": "https://jobs.careers.microsoft.com/global/en/search?lc=India&l=en_us&pg=1&pgSz=20&disp=Advanced",
        "keyword_filter": True
    },
    {
        "name": "Amazon",
        "url": "https://www.amazon.jobs/en/search?base_query=intern&loc_query=India",
        "keyword_filter": True
    },
    {
        "name": "Apple",
        "url": "https://jobs.apple.com/en-in/search?sort=relevance&search=intern&location=india-IND",
        "keyword_filter": True
    },
    {
        "name": "NVIDIA",
        "url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite?q=intern&locations=f2e609fe92974a55a05fc1cdc2852122",
        "keyword_filter": True
    },
    {
        "name": "Uber",
        "url": "https://www.uber.com/us/en/careers/list/?query=intern&location=IND",
        "keyword_filter": True
    }
]

for c in new_companies:
    if c['name'] not in existing:
        config['custom_pages'].append(c)

with open('config.yaml', 'w') as f:
    yaml.dump(config, f, sort_keys=False)

print("Updated config.yaml")
