"""Reproduce a reviewed listing comparison locally without fetching portal pages."""
import argparse
import json
from pathlib import Path

from matching import match, format_result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--listing', default='examples/seslia-listing.json')
    parser.add_argument('--database', default='data/units.sqlite')
    parser.add_argument('--output', default='data/listing-test-result.json')
    args = parser.parse_args()
    listing = json.loads(Path(args.listing).read_text())
    result = match(args.database, listing['facts'], all_candidates=True)
    report = {'listing': listing['url'], 'facts': listing['facts'], 'result': result}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, indent=2))
    print(format_result(result))
