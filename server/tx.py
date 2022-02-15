def ref_addresscoins():

    with open('data/addresses', 'r') as f:
        addresses=f.readlines()

    for address in addresses:
        address=address.replace('\n','')
        with open('data/addresses_coins', 'r') as f:
            addresses_coins = f.read()
        with open('data/addresses_coins', 'a') as f:
            if address not in addresses_coins:
                f.write(address+':'+'0\n')