# Company Insight Extractor

**Version:** 1.0  
**Author:** Afshaan Khan  
**Language:** Python 3.9+  

---

## Overview
**Company Insight Extractor (CIE)** is an automated enrichment and data intelligence tool that gathers publicly available company information from the web.  
By simply providing one or more URLs — such as company websites, Crunchbase pages, or Wellfound profiles — it extracts key insights like:

- Company name, description, and founding year  
- CEO and Founder names (if detected)  
- Publicly available same-domain emails  
- Location and headquarters hints  
- Social media links (LinkedIn, X/Twitter, etc.)  
- Structured output exported to `output.xlsx`

CIE is ideal for **lead research, CRM enrichment, business analytics, and competitive intelligence**.

---

## Features

- **Official Website Detection:** Finds a company’s real site using DuckDuckGo.  
- **Focused Crawling:** Visits key pages (`/about`, `/team`, `/leadership`, etc.).  
- **Smart Parsing:** Extracts data from JSON-LD, meta tags, and visible text.  
- **Email Role Detection:** Associates emails with nearby “CEO” or “Founder” keywords.  
- **Excel Export:** Automatically creates a structured `output.xlsx`.  
- **Configurable:** Adjustable timeouts, limits, and crawl paths.  
- **Transparent:** Prints debug logs and summaries for traceability.

---

## Installation

### Prerequisites
- Python 3.9 or newer  
- pip package manager  

### Setup
```bash
git clone https://github.com/afshaankhan/company-insight-extractor.git
cd company-insight-extractor

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -U pip
pip install requests beautifulsoup4 lxml duckduckgo-search tldextract python-slugify pandas openpyxl readability-lxml
