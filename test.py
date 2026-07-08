try: raise TypeError('hi')
except AttributeError, TypeError: print('CAUGHT')
