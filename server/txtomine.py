import find_coins_amount

def vai(amount,sender,dest, initialcoins):

    if sender=='mined':

        with open('data/addresses_coins', 'r') as f:
            lines=f.readlines()

        with open('data/addresses_coins', 'w') as f:
            for e in lines:
                if dest in e:
                    destfull=e 
                else:
                    f.write(e)

        destok=destfull
        pos=destok.rfind(':')

        destok=destok[:pos]
        with open('data/addresses_coins', 'a') as f:
            testo=destok+':'+str(int(amount)+int(initialcoins))+'\n'
            f.write(testo)
        return

# una volta minata
    with open('data/addresses_coins', 'r') as f:
        lines=f.readlines()
    with open('data/addresses_coins', 'w') as f:
        for e in lines:
            if sender in e:
                pass 
            else:
                f.write(e)
    with open('data/addresses_coins', 'a') as f:
        testo=sender+':'+str(int(initialcoins)-int(amount))+'\n'
        f.write(testo)


    with open('data/addresses_coins', 'r') as f:
        acoins=f.read()
    if dest in acoins:
        coins=find_coins_amount.find(dest)

    with open('data/addresses_coins', 'r') as f:
        lines=f.readlines()

    with open('data/addresses_coins', 'w') as f:
        for e in lines:
            if dest in e:
                destfull=e 
            else:
                f.write(e)

    destok=destfull
    pos=destok.rfind(':')

    destok=destok[:pos]
    #destok=destok[pos+1:]
    print(destok)
    with open('data/addresses_coins', 'a') as f:
        try:
            testo=destok+':'+str(int(coins)+int(amount))+'\n'
            f.write(testo)
        except:
            coins=0
            testo=destok+':'+str(int(coins)+int(amount))+'\n'
            f.write(testo)

    return