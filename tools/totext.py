import sys,re,html
from html.parser import HTMLParser
class P(HTMLParser):
    def __init__(s): super().__init__(); s.out=[]; s.skip=0
    def handle_starttag(s,t,a):
        if t in('script','style','svg'): s.skip+=1
    def handle_endtag(s,t):
        if t in('script','style','svg'): s.skip-=1
    def handle_data(s,d):
        if not s.skip:
            d=d.strip()
            if d: s.out.append(d)
def text(path):
    p=P(); p.feed(open(path,encoding='utf-8',errors='ignore').read()); return p.out
if __name__=='__main__':
    lines=text(sys.argv[1])
    try: i=lines.index('DIVISIONS')
    except ValueError: i=0
    print('\n'.join(lines[i:i+int(sys.argv[2]) if len(sys.argv)>2 else None]))
