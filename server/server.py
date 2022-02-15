from flask import Flask,render_template,request
from random import randint, shuffle
import addrgen
import find_coins_amount
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
            return(address)

        if 'refresh' in request_data:
            address=request_data[7:23]
            acoins=find_coins_amount.find(address)
            return(acoins)
        
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
            with open('data/addresses/'+addrminer, 'r') as f:
                testo=f.read()
                nuovotesto=testo[:65]+str(initialcoins+1)

            with open('data/addresses/'+addrminer, 'w') as f:
                f.write(nuovotesto)

            return(str(initialcoins+1))



        if 'send' in request_data:
            
            pos=request_data.find('#')
            amount=int(request_data[4:pos])
            request_data=request_data[pos+1:]

            pos1=request_data.find(':')
            pos2=request_data.find('#')

            sender=request_data[:pos1]
            senderhash=request_data[pos1+1:pos2]
            dest=request_data[pos2+1:]


            scoins=int(find_coins_amount.find(sender))
            try:
                dcoins=int(find_coins_amount.find(dest))
            except:
                print('Sender or destinatary do not exist')
                return('Sender or destinatary do not exist')
            sncoins=scoins-amount
            dncoins=dcoins+amount
            
            if amount>=scoins:
                print("You can't send more than what you have")
                return("You can't send more than what you have")                
            if amount==0:
                return("You can't send 0")

            
            with open('data/addresses/'+sender, 'r') as f:
                testo=f.read()
                hash=testo[:64]
                nuovotesto=testo[:65]+str(sncoins)
            
            if hash!=senderhash:
                print('figlio di puttana')
                return('figlio di puttana')
            
            with open('data/addresses/'+sender, 'w') as f:
                f.write(nuovotesto)
                
            with open('data/addresses/'+dest, 'r') as f:
                testo=f.read()
                nuovotesto=testo[:65]+str(dncoins)

            with open('data/addresses/'+dest, 'w') as f:
                f.write(nuovotesto)
            
            return('Sent')

        return('error')



if __name__ == "__main__":
 #app.run(host='0.0.0.0',debug=True)
 app.run(host='0.0.0.0',debug=True)
