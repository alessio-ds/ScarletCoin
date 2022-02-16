import random
import string
 
def gen(request_data):
    c=0
    while c==0:
        output_string = ''.join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(16))
        try:
            # provo a vedere se l'address esiste già. se esiste, il ciclo riparte
            with open('data/addresses/'+output_string,'r') as f:
                print('That address already exists. Generating another one right up...')
        except:
            with open('data/addresses/'+output_string,'w') as f:
                f.write(request_data+':0')
                c+=1
            with open('data/address', 'a') as f:
                f.write(request_data)
            return(output_string)

def gentxid(sender,dest,amount):
    output_string = ''.join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(32))
    with open('data/txs','r') as f:
        txs=f.read()
    if output_string not in txs:
        with open('data/txs','a') as f:
            f.write(output_string+':'+sender+':'+dest+':'+str(amount)+'\n')
    return(output_string)
