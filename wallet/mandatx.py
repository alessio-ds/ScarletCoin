import requests

def manda(amount, address_sender, address_destinatario):
    data='send'+amount+'#'+address_sender+'#'+address_destinatario
    with open('data/server.txt', 'r') as f:
        url=f.read()

    response=requests.post(url,data)
    response=response.text
    return response