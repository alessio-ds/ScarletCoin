def find(address):
    print(address)
    with open('data/addresses_coins', 'r') as f:
        ac=f.read()

    pos=ac.find(address)+82
    ac=ac[pos:]
    pos=ac.find('\n')

    return(ac[:pos])

