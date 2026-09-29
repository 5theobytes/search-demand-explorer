# Search Demand Explorer

A small Flask app for exploring search demand. Enter seed keywords, choose a country and language, then compare related-search ideas from Google Ads, DataForSEO Labs, Google Autocomplete, Google Trends, and Google SERP.

The interface is currently in Russian.

## Features

- Check monthly search volume for up to 1,000 keywords.
- Discover related search directions from selectable sources.
- Limit the merged result set to up to 5,000 keywords.
- Control the related-keyword depth for DataForSEO Labs results.
- Merge duplicate phrases while retaining their sources and seed keywords.
- Review request costs, save selected phrases to a working basket, and export results to CSV.

## Run locally

Requirements: Python 3.10 or newer.

```bash
python -m venv .venv
```

Activate the environment, install dependencies, and start the app:

```bash
# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# Windows cmd
.venv\Scripts\activate.bat

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. On Windows, `start.bat` can install dependencies and launch the app.

## Data and costs

A DataForSEO account is required for data requests. Use the app only on a deployment you trust. Requests may incur charges under your DataForSEO plan; review the provider's pricing before use.

## Deploy

The repository includes a Vercel configuration. Import it into Vercel to deploy.

## License

MIT. See [LICENSE](LICENSE).
