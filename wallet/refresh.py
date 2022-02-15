import requests
def ref(address):
    with open('data/server.txt', 'r') as f:
        url=f.read()
    data='refresh'+address
    response=requests.post(url,data)
    response=response.text
    return response
