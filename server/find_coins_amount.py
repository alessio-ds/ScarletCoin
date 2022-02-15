def find(address):
    if address=='\n':
        return
    with open('data/addresses/'+address, 'r') as f:
        rcoins=f.read()
        ac=rcoins[65:]
    return(ac)
