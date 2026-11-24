"""Prepare X monitoring rules; --apply requires operator-provided X API access.

Only adds this project's missing rules. Never deletes third-party rules.
"""
import argparse,json,os,urllib.request
from pathlib import Path

def build_rules(accounts):
    rules=[]
    for start in range(0,len(accounts),12):
        value='('+' OR '.join('from:'+a['handle'] for a in accounts[start:start+12])+') -is:retweet'
        rules.append({'value':value,'tag':f'analizator-watch-{start//12+1}'})
    return rules

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    accounts=json.loads((Path(__file__).parent/'config/x_watchlist.json').read_text(encoding='utf-8'))['accounts']
    rules=build_rules(accounts)
    if not args.apply:
        print(json.dumps({'add':rules},ensure_ascii=False,indent=2));return
    token=os.getenv('X_BEARER_TOKEN')
    if not token: raise SystemExit('Set X_BEARER_TOKEN locally. X API access is required; no token is printed.')
    url='https://api.x.com/2/tweets/search/stream/rules'
    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
    try:
        existing=json.load(urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=10))
        known={r['value'] for r in existing.get('data',[])}
        missing=[r for r in rules if r['value'] not in known]
        if missing:
            request=urllib.request.Request(url,data=json.dumps({'add':missing}).encode(),headers=headers,method='POST')
            result=json.load(urllib.request.urlopen(request,timeout=10))
            if result.get('errors'): raise SystemExit('X rejected one or more rules. Check access tier and rule limits.')
        print('X rules are configured. Account numeric IDs still require independent verification.')
    except urllib.error.HTTPError as error:
        raise SystemExit(f'X API returned HTTP {error.code}; verify access and plan limits.') from None
if __name__=='__main__': main()
