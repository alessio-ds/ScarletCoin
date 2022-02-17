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