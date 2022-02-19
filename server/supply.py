import os
import find_coins_amount

def findsupply():
    supply=0
    dir=os.getcwd()
    dir=dir+'/data/addresses'
    for filename in os.listdir(dir):
        coins=find_coins_amount.find(filename)
        if coins!='Address does not exist' and coins!='':
            supply=supply+int(find_coins_amount.find(filename))
    return(supply)


def findtop():
    #lista_addresses=[]
    diz_addresses={}
    dir=os.getcwd()
    dir=dir+'/data/addresses'
    for filename in os.listdir(dir):
        coins=find_coins_amount.find(filename)
        if coins!='Address does not exist' and coins!='':
            if coins!='0':
                diz_addresses[filename]=int(coins)

    diz_list = sorted(diz_addresses.items(), key=lambda x:x[1])
    diz_sorted = dict(diz_list)
    diz_sorted = dict(reversed(list(diz_sorted.items())))
    return(diz_sorted)
    

findtop()