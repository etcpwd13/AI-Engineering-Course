#!/usr/bin/env python3
import os
import sys
import json
import logging
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv
import openai
from datetime import datetime

# Setup logging
logging.basicConfig(level=logging.INFO)

# ANSI color codes
RED = "\033[1;31m"
GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
BLUE = "\033[1;34m"
MAGENTA = "\033[1;35m"
CYAN = "\033[1;36m"
RESET = "\033[0m"

# Define HTTP headers and (optionally) proxies
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/117.0.0.0 Safari/537.36"
    )
}
# Uncomment and set your proxies if needed:
# PROXIES = {"http": "http://your-proxy", "https": "http://your-proxy"}
PROXIES = None

# File where the sites list is persisted
SITES_FILE = "sites.json"


def load_sites():
    """
    Load sites from the SITES_FILE. If the file doesn't exist, create it with default sites.
    """
    if not os.path.exists(SITES_FILE):
        default_sites = {
            "1": {"url": "https://www.cybersecurity-insiders.com/", "name": "Cybersecurity Insiders"},
            "2": {"url": "https://www.darkreading.com/", "name": "Dark Reading"},
            "3": {"url": "https://www.infosecurity-magazine.com/", "name": "Infosecurity Magazine"},
            "4": {"url": "https://cnn.com/", "name": "CNN"}
        }
        save_sites(default_sites)
        return default_sites
    else:
        try:
            with open(SITES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error reading {SITES_FILE}: {e}")
            sys.exit(1)


def save_sites(sites):
    """
    Save the current sites dictionary to SITES_FILE.
    """
    try:
        with open(SITES_FILE, 'w', encoding='utf-8') as f:
            json.dump(sites, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving sites to {SITES_FILE}: {e}")


def fetch_with_selenium(url):
    """
    Use Selenium (with headless Chrome) to fetch pages that require JavaScript.
    Make sure selenium and the appropriate WebDriver are installed.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        logging.error("Selenium is not installed. Please install selenium to use JS-heavy page support.")
        return None

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    try:
        driver = webdriver.Chrome(options=options)
    except Exception as e:
        logging.error(f"Error initializing Selenium WebDriver: {e}")
        return None

    try:
        driver.get(url)
        html = driver.page_source
    except Exception as e:
        logging.error(f"Error fetching page with Selenium: {e}")
        html = None
    finally:
        driver.quit()
    return html


def sanitize_filename(filename):
    """
    Sanitize the filename by removing characters not allowed in file names.
    """
    return re.sub(r'[\\/*?:"<>|]', "", filename)


def is_navigation_link(a_tag):
    """
    Return True if the <a> tag is inside a navigation element (nav, header, or footer).
    """
    for parent in a_tag.parents:
        if parent.name in ['nav', 'header', 'footer']:
            return True
    return False


class Website:
    """
    Represents a webpage. Downloads and parses the page with BeautifulSoup,
    extracts the title, text, and also saves all URL links that appear to be part of the main story.
    If the initial request returns little content, attempts to use Selenium for JavaScript-heavy pages.
    """
    def __init__(self, url):
        self.url = url
        # Try to fetch using requests
        try:
            response = requests.get(url, headers=headers, timeout=10, proxies=PROXIES)
            response.raise_for_status()
        except requests.RequestException as e:
            logging.error(f"Error fetching the URL via requests: {e}")
            sys.exit(1)

        content_type = response.headers.get('Content-Type', '')
        if 'html' not in content_type:
            logging.error("The URL did not return HTML content.")
            sys.exit(1)

        soup = BeautifulSoup(response.content, 'html.parser')
        self.title = soup.title.string.strip() if soup.title and soup.title.string else "No Title"
        self.links = []
        if soup.body:
            # Extract <a> tags only if they are not inside nav, header, or footer elements.
            for a in soup.body.find_all('a', href=True):
                if is_navigation_link(a):
                    continue
                link_text = a.get_text().strip() or a['href']
                link_url = a['href']
                if not link_url.startswith("http"):
                    link_url = urljoin(self.url, link_url)
                self.links.append((link_text, link_url))
            # Now remove unwanted tags.
            for tag in soup.body(["script", "style", "img", "input", "nav", "footer", "header"]):
                tag.decompose()
            self.text = soup.body.get_text(separator="\n", strip=True)
        else:
            self.text = ""

        # If the extracted text is very short, try Selenium as a fallback.
        if not self.text.strip() or len(self.text) < 50:
            logging.info("Page content appears minimal, trying Selenium for JavaScript-heavy content...")
            html = fetch_with_selenium(url)
            if html:
                soup = BeautifulSoup(html, 'html.parser')
                if soup.body:
                    self.links = []
                    for a in soup.body.find_all('a', href=True):
                        if is_navigation_link(a):
                            continue
                        link_text = a.get_text().strip() or a['href']
                        link_url = a['href']
                        if not link_url.startswith("http"):
                            link_url = urljoin(self.url, link_url)
                        self.links.append((link_text, link_url))
                    for tag in soup.body(["script", "style", "img", "input", "nav", "footer", "header"]):
                        tag.decompose()
                    self.text = soup.body.get_text(separator="\n", strip=True)
                else:
                    self.text = ""


def user_prompt_for(website, max_chars=4000):
    """
    Build a robust prompt for summarization including clear instructions.
    If the text is too long, only the first max_chars characters are included.
    """
    prompt = f"You are analyzing the website titled '{website.title}'.\n"
    prompt += (
        "Please provide a robust and comprehensive summary in markdown. "
        "Include all key points, background details, and any news or announcements present. "
        "Your summary should be complete and detailed enough to give a full understanding of the website's content.\n\n"
    )
    if len(website.text) > max_chars:
        prompt += website.text[:max_chars] + "\n\n[Content truncated]"
    else:
        prompt += website.text
    return prompt


def messages_for(website, system_prompt):
    """
    Build the list of messages in the format required by the OpenAI Chat Completion API.
    """
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt_for(website)}
    ]


def summarize(url, model):
    """
    Given a URL, scrape the website and get a robust summary from the OpenAI API.
    Returns both the Website object and the summary.
    """
    website = Website(url)
    system_prompt = (
        "You are an assistant that analyzes website content and provides a robust, complete, and comprehensive summary in markdown. "
        "Include key details, context, and highlight any news or announcements."
    )
    try:
        response = openai.chat.completions.create(
            model=model,
            messages=messages_for(website, system_prompt)
        )
    except openai.error.OpenAIError as e:
        logging.error(f"OpenAI API error: {e}")
        sys.exit(1)
    summary = response.choices[0].message.content
    return website, summary


def format_summary_for_file(website, summary):
    """
    Prepend the summary with a header that includes the title, current date, and attribution.
    Then append a "Links" section in markdown format if any main-story links were extracted.
    The header format is:
        Title
        Date: <today date>
        By: GreyFriar

    Followed by a blank line, the summary content, and finally the links section.
    """
    current_date = datetime.now().strftime('%Y-%m-%d')
    header = f"{website.title}\nDate: {current_date}\nBy: GreyFriar\n\n"
    content = header + summary
    if website.links:
        content += "\n\n## Links\n"
        for text, url in website.links:
            content += f"- [{text}]({url})\n"
    return content


def save_summary(website, summary, custom_name=None):
    """
    Save the formatted summary to a local file.
    The filename contains the current date and a sanitized version of either the website title or a custom name.
    """
    current_date = datetime.now().strftime('%Y-%m-%d')
    content = format_summary_for_file(website, summary)
    if custom_name:
        filename = f"{current_date}_{sanitize_filename(custom_name)}.md"
    else:
        filename = f"{current_date}_{sanitize_filename(website.title)}.md"
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(content)
        logging.info(f"Summary saved to file: {filename}")
        print(f"{GREEN}Summary saved to file: {filename}{RESET}")
    except Exception as e:
        logging.error(f"Error saving summary to file: {e}")


def choose_output_destination():
    """
    Ask the user whether they want to see the result on screen, save to file, or both.
    """
    print(f"{YELLOW}Where do you want the result to go?{RESET}")
    print(f"{GREEN}[1]{RESET} Screen")
    print(f"{GREEN}[2]{RESET} File")
    print(f"{GREEN}[3]{RESET} Both")
    option = input(f"{BLUE}Enter your choice (1, 2, or 3): {RESET}").strip()
    return option


def daily_summary(sites, model):
    """
    Generate a combined daily summary from all persisted sites.
    Each site's summary is prefixed with its header.
    The final output is saved to a file and/or printed based on user choice.
    """
    combined_summary = f"# Daily Summary for {datetime.now().strftime('%Y-%m-%d')}\n\n"
    for key in sorted(sites, key=lambda x: int(x)):
        site_info = sites[key]
        url = site_info["url"]
        print(f"{CYAN}Processing {url}... Please wait...{RESET}")
        website, summary = summarize(url, model)
        header = f"{website.title}\nDate: {datetime.now().strftime('%Y-%m-%d')}\nBy: GreyFriar\n\n"
        combined_summary += header + summary + "\n\n---\n\n"
    filename = f"{datetime.now().strftime('%Y-%m-%d')}_Daily_Summary.md"
    option = choose_output_destination()
    if option in ["2", "3"]:
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(combined_summary)
            logging.info(f"Daily summary saved to file: {filename}")
            print(f"{GREEN}Daily summary saved to file: {filename}{RESET}")
        except Exception as e:
            logging.error(f"Error saving daily summary: {e}")
    if option in ["1", "3"]:
        print(f"\n{YELLOW}Daily Summary:{RESET}\n")
        print(combined_summary)


def print_welcome_menu(sites):
    """
    Print the welcome screen with ASCII art and the colored menu options.
    """
    header = f"""{CYAN}
     _____               ______    _            
    |  __ \              |  ___|  (_)           
    | |  \/_ __ ___ _   _| |_ _ __ _  __ _ _ __ 
    | | __| '__/ _ \ | | |  _| '__| |/ _` | '__|
    | |_\ \ | |  __/ |_| | | | |  | | (_| | |   
     \____/_|  \___|\__, \_| |_|  |_|\__,_|_|   
                    __/ |                      
                   |___/   
             Cyber Security News                    
{RESET}"""
    print(header)
    print(f"{YELLOW}Please choose an option:{RESET}")
    for key in sorted(sites, key=lambda x: int(x)):
        site_info = sites[key]
        print(f"{GREEN}[{key}]{RESET} Summarize {site_info['url']} ({site_info['name']})")
    print(f"{GREEN}[5]{RESET} Enter your own site URL")
    print(f"{GREEN}[6]{RESET} Generate a daily summary from all default sites")
    print(f"{GREEN}[7]{RESET} Add another site to the default sites list")
    print(f"{GREEN}[8]{RESET} Remove a site from the default sites list")
    print(f"{GREEN}[0]{RESET} End/Exit the program")
    print()


def main():
    # Load environment variables from the .env file.
    load_dotenv(override=True)
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not api_key.strip():
        logging.error("No valid API key found. Please check your .env file.")
        sys.exit(1)
    openai.api_key = api_key
    logging.info("API key loaded successfully.")

    # Set the default model to use.
    model = "gpt-4o-mini"  # Adjust as needed

    # Load persisted sites from file.
    sites = load_sites()

    while True:
        print_welcome_menu(sites)
        choice = input(f"{BLUE}Enter your choice: {RESET}").strip().lower()
        if choice in ["0", "end"]:
            print(f"{MAGENTA}Exiting. Goodbye!{RESET}")
            break
        elif choice in sites:
            site_info = sites[choice]
            url = site_info["url"]
            print(f"{CYAN}Processing {url}... Please wait...{RESET}")
            website, summary = summarize(url, model)
            option = choose_output_destination()
            if option in ["1", "3"]:
                print(f"\n{YELLOW}Summary:{RESET}\n")
                print(summary)
            if option in ["2", "3"]:
                save_summary(website, summary)
        elif choice == "5":
            url = input(f"{BLUE}Enter the site URL: {RESET}").strip()
            if url:
                print(f"{CYAN}Processing {url}... Please wait...{RESET}")
                website, summary = summarize(url, model)
                option = choose_output_destination()
                if option in ["1", "3"]:
                    print(f"\n{YELLOW}Summary:{RESET}\n")
                    print(summary)
                if option in ["2", "3"]:
                    save_summary(website, summary)
            else:
                print(f"{RED}No URL provided.{RESET}")
        elif choice == "6":
            daily_summary(sites, model)
        elif choice == "7":
            new_url = input(f"{BLUE}Enter the URL to add: {RESET}").strip()
            if not new_url:
                print(f"{RED}No URL provided. Returning to menu.{RESET}")
                continue
            new_name = input(f"{BLUE}Enter a name for this site (optional): {RESET}").strip()
            if not new_name:
                new_name = new_url  # fallback if no name provided
            next_key = str(max([int(k) for k in sites.keys()] + [0]) + 1)
            sites[next_key] = {"url": new_url, "name": new_name}
            save_sites(sites)
            print(f"{GREEN}Site added successfully as option [{next_key}].{RESET}")
        elif choice == "8":
            # List current sites and ask which one to remove.
            print(f"{YELLOW}Current sites:{RESET}")
            for key in sorted(sites, key=lambda x: int(x)):
                print(f"{GREEN}[{key}]{RESET} {sites[key]['name']} ({sites[key]['url']})")
            rem_key = input(f"{BLUE}Enter the option number to remove (or press Enter to cancel): {RESET}").strip()
            if rem_key in sites:
                removed = sites.pop(rem_key)
                save_sites(sites)
                print(f"{GREEN}Removed site: {removed['name']} ({removed['url']}).{RESET}")
            else:
                print(f"{RED}Invalid option. No site removed.{RESET}")
        else:
            print(f"{RED}Invalid choice. Please try again.{RESET}")

if __name__ == '__main__':
    main()
