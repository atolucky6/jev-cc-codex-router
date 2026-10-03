"""Check inline dashboard JavaScript without executing it or loading secrets."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import tempfile


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            attrs = dict(attrs)
            self.active = not attrs.get('src') and attrs.get('type', '') in (
                '', 'text/javascript', 'application/javascript', 'module'
            )

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.chunks.append(data)


if __name__ == '__main__':
    parser = Scripts()
    parser.feed(Path('dashboard.html').read_text(encoding='utf-8'))
    assert parser.chunks, 'No dashboard scripts found'
    with tempfile.TemporaryDirectory() as directory:
        for i, chunk in enumerate(parser.chunks):
            path = Path(directory) / ('script%d.mjs' % i)
            path.write_text(chunk, encoding='utf-8')
            subprocess.run(['node', '--check', str(path)], check=True)
    print('Dashboard JavaScript syntax OK')
