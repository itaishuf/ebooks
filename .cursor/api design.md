## Architecture 
### Book discovery
Get user input and return book id
### Downloader module
Input: book isbn
Logic: send book to different downloading modules, orchestrate them, validate the file output
Output: a path to the downloaded book
### Sender module
Sending the book to kindle mail
## Tests
Definition of done: all tests are passing
### file type fallback
Provide as config a book name that you know has pdf and epub links, and a book name that only has pdf. 
Pass when book number 1 is downloaded as epub and book 2 is downloaded as pdf. Make sure it has a log stating tha epub fetching has failed. 
Check in the downloads folder the file existence to get the test resul

### full e2e test
Check the entire flow from searching up a new book until sending ti kindle. 
Pass when itaishuf@gmail.com has a new mail in the lase sent items with an attachment if type epub or pdf. To validate the file content,  it must have inside it the same string used to search up the book in goodreads. 

## Security
This api will be available from the wide internet so it must hold up to the most advanced security standards. 
The deployment is behind a tls terminating reverse proxy (tailscale services and tailscale funnel)
Use pydantic to validate all user input