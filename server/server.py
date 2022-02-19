from flask import Flask,render_template,request,redirect
import addrgen
import find_coins_amount
import hashlib
import datetime
from threading import Thread
from time import sleep
import supply

app = Flask(__name__)

@app.route('/', methods = ['POST', 'GET'])
def data():
    if request.method == 'GET':
        return render_template('index.html')
    if request.method == 'POST':
        try:
            form_data = request.form
            a=form_data
            txid=a['Field1_name']
            txid=txid.strip()
            try:
                with open('templates/txs/'+txid+'.html', 'r') as f:
                    print(f'{txid} has been found') 
                    with open('data/explorer','r') as f:
                        explurl=f.read()
                        explurl=explurl.strip()
                    return redirect(explurl+txid+'.html')
            except:
                return("Couldn't find any tx with that id.")

        except:
            pass #significa che la richiesta non è stata fatta al sito
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
            addrminer=stringa[pos+1:]
            addrminer=addrminer.strip('\n')
            #sender='0000000000000000:9444d1539ffadec30fc6b0f6386d87ff36d58f9a92884253bd9c0c7b4c20e80e'
            coins=find_coins_amount.find(addrminer)
            if coins=='Address does not exist':
                print('address doesnt exist')
                return(coins)
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

            if sender==dest:
                return("You can't send money to yourself")

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
            
            ##### generazione pagina web

            txid=addrgen.gentxid(sender,dest,amount)
            with open('templates/txs/'+txid+'.html', encoding='utf-8', mode='w') as f:
                with open('templates/txbase1.html', encoding='utf-8', mode='r') as a:
                    primaparte=a.read()
                with open('templates/txbase2.html', encoding='utf-8', mode='r') as a:
                    secondaparte=a.read()

                now = datetime.datetime.now()
                date=now.strftime('%Y-%m-%d %H:%M:%S')
                fstr=f"{primaparte}TXID  {txid}」<br><br>「{sender} sent {amount} ScarletCoins to {dest}」<br>「Date of tx: {date}」<br><br>「<a href='http://alessiosca.ddns.net:20000'>Go Back</a>{secondaparte}"
                f.write(fstr)
            

            return(f'Sent. \nTxid = {txid}')

        if 'request_txs' in request_data:
            addr_to_check=request_data[11:] #11 è la lenght di 'request_txs'
            data_to_send=''
            with open('data/txs', 'r') as f:
                txs=f.readlines()
            for e in txs:
                if addr_to_check in e:
                    data_to_send+=e+'#'
            print(data_to_send)
            return(data_to_send)

            
        if 'import' in request_data:
            hash=request_data[6:]
            with open('data/address', 'r') as f:
                linee = f.readlines()
            for l in linee:
                l=l.strip()
                if l[17:]==hash:
                    return(l)
            return('non existant')
            
       
        return('error')
    

def start():
    t1=Thread(target=run)
    t1.start()


def run():
    while True:
        diz_top=supply.findtop()
        stringa_diztop=''
        cont=0
        for e in diz_top:
            if cont<=10:
                cont+=1
                stringa_diztop+=f'{cont}. 「{e} — {diz_top[e]} ScarletCoins」<br>'
        with open('templates/indexbase1.html','r', encoding='utf-8') as f:
            i1=f.read()
        with open('templates/indexbase2.html','r', encoding='utf-8') as f:
            i2=f.read()
        with open('templates/index.html','w', encoding='utf-8') as f:
            stronzium=f'<h4><font font color="#c1b492">Top ten addresses:<br><br>{stringa_diztop}</font><h2><font font color="#c1b492"><br>Total supply: {supply.findsupply()} </font> </h2>'
            index=i1+stronzium+i2
            f.write(index)


        sleep(300)

if __name__ == "__main__":
    start()
    app.run(host='0.0.0.0', debug=True)
