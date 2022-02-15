import random
import string
 
def gen(request_data):
    output_string = ''.join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(16))
    with open('data/addresses/'+output_string,'w') as f:
        f.write(request_data+':0')
    return(output_string)