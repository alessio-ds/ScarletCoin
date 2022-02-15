from flask import Flask,render_template,request
from random import randint, shuffle
import addrgen
import tx
import find_coins_amount
import txtomine
import hashlib

app = Flask(__name__)

@app.route('/', methods = ['POST', 'GET'])
def data():
    if request.method == 'GET':
        return f"You can't access this URL directly"
    if request.method == 'POST':
        request_data = request.data
        request_data = request_data.decode('utf-8')
        print(request_data)

        if len(request_data)==64:         # req data = hash sha256
            address=addrgen.gen(request_data)
            print(address)
            tx.ref_addresscoins()
            return address

        if 'refresh' in request_data:
            tx.ref_addresscoins()
            with open('data/addresses_coins', 'r') as f:
                acoins=f.read()
            addr_from_request_data=request_data[7:]
            if addr_from_request_data in acoins:
                coins=find_coins_amount.find(addr_from_request_data)
                return(coins)
            return('error')
        
        if 'mined' in request_data:
            stringa=request_data[5:]
            pos=stringa.find('#')
            strhash=stringa[:pos]
            print(strhash)
            strhashok=(hashlib.sha256(strhash.encode())).hexdigest()
            if strhashok[0:4]!='0000':
                print(strhashok)
                print('nonvalid')
                return('Hash is not valid')
            with open('data/mined','r') as f:
                mined=f.read()
            if strhash in mined:
                print('already mined')
                return('Already mined')
            else:
                with open('data/mined','a') as f:
                    f.write(strhash+'\n')
            print('valid')
            addrminer=stringa[pos+1:]
            #sender='0000000000000000:9444d1539ffadec30fc6b0f6386d87ff36d58f9a92884253bd9c0c7b4c20e80e'
            coins=find_coins_amount.find(addrminer)
            initialcoins=int(coins)
            sender='mined'
            txtomine.vai('1',sender,addrminer, initialcoins)



        if 'send' in request_data:
            tx.ref_addresscoins()
            
            pos=request_data.find('#')
            amount=request_data[4:pos]
            request_data=request_data[pos+1:]

            pos=request_data.find('#')

            sender=request_data[:pos]

            dest=request_data[pos+1:]


            tx.ref_addresscoins()
            with open('data/addresses_coins', 'r') as f:
                acoins=f.read()
            if sender[:16] in acoins:
                coins=find_coins_amount.find(sender[:16])
            coins=int(coins)
            amount=int(amount)

            with open('data/addresses_coins', 'r') as f:
                lines=f.read()
                if sender not in lines or dest not in lines:
                    print('Sender or destinatary do not exist')
                    return('Sender or destinatary do not exist')
            if amount>=coins:
                print("You can't send more than what you have")
                return("You can't send more than what you have")
            if amount==0:
                return("You can't send 0")
            
            initialcoins=coins
            txtomine.vai(amount,sender,dest, initialcoins)
            return('Sent')

        return('error')



if __name__ == "__main__":
 #app.run(host='0.0.0.0',debug=True)
 app.run(host='0.0.0.0',debug=True)
