def find(address):
    if address=='\n':
        return
    try:
        with open('data/addresses/'+address, 'r') as f:
            rcoins=f.read()
            ac=rcoins[65:]
        return(ac)
    except:
        return('Address does not exist')