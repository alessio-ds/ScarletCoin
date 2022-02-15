import requests
import random, hashlib, string

def askaddr():
    output_string = ''.join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(16))
    hashed_string = hashlib.sha256(output_string.encode('utf-8')).hexdigest()

    print(hashed_string)

    with open('data/server.txt', 'r') as f:
        url=f.read()
    addr=requests.post(url, hashed_string)

    # addr = address epic, 16 char
    print(addr.text)
    # se la risposta del server non è un cazzo di address da 16 caratteri, qualcosa si è rotto
    if len(addr.text)!=16:
        print('holy shit')
        return

    with open('data/addresslist.txt', 'a') as f:
        f.write(addr.text+':'+hashed_string+'\n')
    return addr.text
