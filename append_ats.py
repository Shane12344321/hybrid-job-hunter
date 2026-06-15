import yaml

with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)

existing = {c['name'] for c in config.get('ats_companies', [])}

new_companies = [
    {"name": "LinkedIn", "ats": "greenhouse", "slug": "linkedin"},
    {"name": "Tower Research", "ats": "greenhouse", "slug": "towerresearchcapital"},
    {"name": "WorldQuant", "ats": "greenhouse", "slug": "worldquant"},
    {"name": "MongoDB", "ats": "greenhouse", "slug": "mongodb"},
    {"name": "Confluent", "ats": "ashby", "slug": "confluent"},
    {"name": "Rubrik", "ats": "greenhouse", "slug": "rubrik"},
    {"name": "Twilio", "ats": "greenhouse", "slug": "twilio"},
    {"name": "Dropbox", "ats": "greenhouse", "slug": "dropbox"},
    {"name": "Zscaler", "ats": "greenhouse", "slug": "zscaler"},
    {"name": "Postman", "ats": "greenhouse", "slug": "postman"},
    {"name": "PhonePe", "ats": "greenhouse", "slug": "phonepe"},
    {"name": "Cred", "ats": "lever", "slug": "cred"},
    {"name": "Thoughtworks", "ats": "greenhouse", "slug": "thoughtworks"},
    {"name": "MindTickle", "ats": "lever", "slug": "mindtickle"},
    {"name": "Groww", "ats": "greenhouse", "slug": "groww"},
]

for c in new_companies:
    if c['name'] not in existing:
        config['ats_companies'].append(c)

with open('config.yaml', 'w') as f:
    yaml.dump(config, f, sort_keys=False)

print("Updated config.yaml")
