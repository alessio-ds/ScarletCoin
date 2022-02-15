def find(address):
    with open('data/addresses/'+address, 'r') as f:
        rcoins=f.read()
        ac=rcoins[65:]
    return(ac)
