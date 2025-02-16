import os
import re
import sys
import time
from vllm_llm import LLM
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS


# Default configuration
NUM_SEARCH = 10  # Number of links to parse from DuckDuckGo
SEARCH_TIME_LIMIT = 3  # Max seconds to request website sources before skipping to the next URL
TOTAL_TIMEOUT = 6  # Overall timeout for all operations
MAX_CONTENT = 500  # Number of words to add to LLM context for each search result


client = LLM()

def get_query():
    """Prompt the user to enter a query."""
    return input("Enter your query: ")

def trace_function_factory(start):
    """Create a trace function to timeout request"""
    def trace_function(frame, event, arg):
        if time.time() - start > TOTAL_TIMEOUT:
            raise TimeoutError('website fetching timed out')
        return trace_function
    return trace_function

def fetch_webpage(url, timeout):
    """Fetch the content of a webpage given a URL and a timeout."""
    start = time.time()
    sys.settrace(trace_function_factory(start))
    try:
        print(f"Fetching link: {url}")
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'lxml')
        paragraphs = soup.find_all('p')
        page_text = ' '.join([para.get_text() for para in paragraphs])
        return url, page_text
    except (requests.exceptions.RequestException, TimeoutError) as e:
        print(f"Error fetching {url}: {e}")
    finally:
        sys.settrace(None)
    return url, None

def duckduckgo_parse_webpages(query, num_search=NUM_SEARCH, search_time_limit=SEARCH_TIME_LIMIT):
    """Perform a DuckDuckGo search and parse the content of the top results."""
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=num_search))
    urls = [result['href'] for result in results]
    max_workers = os.cpu_count() or 1  # Fallback to 1 if os.cpu_count() returns None
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(fetch_webpage, url, search_time_limit): url for url in urls}
        return {url: page_text for future in as_completed(future_to_url) if (url := future.result()[0]) and (page_text := future.result()[1])}


def build_prompt(query, search_dic, max_content=MAX_CONTENT):
    """Build the prompt for the language model including the search results context."""
    context_list = [f"[{i+1}]({url}): {content[:max_content]}" for i, (url, content) in enumerate(search_dic.items())]
    context_block = "\n".join(context_list)
    system_message = f"""
    You are a helpful assistant who is expert at answering user's queries based on the cited context.

    Generate a response that is informative and relevant to the user's query based on provided context (the context consists of search results containing a key with [citation number](website link) and brief description of the content of that page).
    You must use this context to answer the user's query in the best way possible. Use an unbiased and journalistic tone in your response. Do not repeat the text.
    You must not tell the user to open any link or visit any website to get the answer. You must provide the answer in the response itself.
    Your responses should be medium to long in length, be informative and relevant to the user's query. You must use markdown to format your response. You should use bullet points to list the information. Make sure the answer is not short and is informative.
    You have to cite the answer using [citation number](website link) notation. You must cite the sentences with their relevant context number. You must cite each and every part of the answer so the user can know where the information is coming from.
    Anything inside the following context block provided below is for your knowledge returned by the search engine and is not shared by the user. You have to answer questions on the basis of it and cite the relevant information from it but you do not have to 
    talk about the context in your response.
    context block:
    {context_block}
    """
    return [{"role": "system", "content": system_message}, {"role": "user", "content": query}]


def renumber_citations(response):
    """Renumber citations in the response to be sequential."""
    citations = sorted(set(map(int, re.findall(r'\[\(?(\d+)\)?\]', response))))
    citation_map = {old: new for new, old in enumerate(citations, 1)}
    for old, new in citation_map.items():
        response = re.sub(rf'\[\(?{old}\)?\]', f'[{new}]', response)
    return response, citation_map

def generate_citation_links(citation_map, search_dic):
    """Generate citation links based on the renumbered response."""
    cited_links = []
    for old, new in citation_map.items():
        url = list(search_dic.keys())[old-1]
        cited_links.append(f"{new}. {url}")
    return "\n".join(cited_links)

def save_markdown(query, response, search_dic):
    """Renumber citations, then save the query, response, and sources to a markdown file."""
    response, citation_map = renumber_citations(response)
    links_block = generate_citation_links(citation_map, search_dic)
    output_content = (
        f"# {query}\n\n"
        f"## Sources\n{links_block}\n\n"
        f"## Answer\n{response}" 
    )
    file_name = f"{query}.md"
    with open(file_name, "w") as file:
        file.write(output_content)

def main():
    """Main function to execute the search, rerank results, generate response, and save to markdown."""
    query = get_query() 
    search_dic = duckduckgo_parse_webpages(query)
    prompt = build_prompt(query, search_dic)
    response = client.get_chat_completion(prompt)
    save_markdown(query, response, search_dic)

if __name__ == "__main__":
    main()
