import random
import string
 
def gen(request_data):
    output_string = ''.join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(16))
    with open('data/addresses','a') as f:
        f.write(output_string+':'+request_data+'\n')
    return(output_string)