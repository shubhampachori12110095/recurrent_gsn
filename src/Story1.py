import numpy, os, sys, cPickle
import numpy.random as rng
import theano
import theano.tensor as T
import theano.sandbox.rng_mrg as RNG_MRG
import PIL.Image
from collections import OrderedDict
from image_tiler import tile_raster_images
import time
import data_tools as data
from utils import *

def experiment(state, outdir_base='./'):
    rng.seed(1) #seed the numpy random generator  
    data.mkdir_p(outdir_base)
    outdir = outdir_base + "/" + state.dataset + "/"
    data.mkdir_p(outdir)
    logfile = outdir+"log.txt"
    with open(logfile,'w') as f:
        f.write("MODEL 1, {0!s}\n\n".format(state.dataset))
    train_convergence_pre = outdir+"train_convergence_pre.csv"
    train_convergence_post = outdir+"train_convergence_post.csv"
    valid_convergence_pre = outdir+"valid_convergence_pre.csv"
    valid_convergence_post = outdir+"valid_convergence_post.csv"
    test_convergence_pre = outdir+"test_convergence_pre.csv"
    test_convergence_post = outdir+"test_convergence_post.csv"
    recurrent_train_convergence = outdir+"recurrent_train_convergence.csv"
    recurrent_valid_convergence = outdir+"recurrent_valid_convergence.csv"
    recurrent_test_convergence = outdir+"recurrent_test_convergence.csv"
    with open (train_convergence_pre, 'w') as f:
        f.write("")
    with open (train_convergence_post, 'w') as f:
        f.write("")
    with open (valid_convergence_pre, 'w') as f:
        f.write("")
    with open (valid_convergence_post, 'w') as f:
        f.write("")
    with open (test_convergence_pre, 'w') as f:
        f.write("")
    with open (test_convergence_post, 'w') as f:
        f.write("")
    with open (recurrent_train_convergence, 'w') as f:
        f.write("")
    with open (recurrent_valid_convergence, 'w') as f:
        f.write("")
    with open (recurrent_test_convergence, 'w') as f:
        f.write("")
    
    print
    print "----------MODEL 1, {0!s}--------------".format(state.dataset)
    print
    #load parameters from config file if this is a test
    config_filename = outdir+'config'
    if state.test_model and 'config' in os.listdir(outdir):
        config_vals = load_from_config(config_filename)
        for CV in config_vals:
            print CV
            if CV.startswith('test'):
                print 'Do not override testing switch'
                continue        
            try:
                exec('state.'+CV) in globals(), locals()
            except:
                exec('state.'+CV.split('=')[0]+"='"+CV.split('=')[1]+"'") in globals(), locals()
    else:
        # Save the current configuration
        # Useful for logs/experiments
        print 'Saving config'
        with open(config_filename, 'w') as f:
            f.write(str(state))


    print state
    artificial = False
    if state.dataset == 'MNIST_1' or state.dataset == 'MNIST_2' or state.dataset == 'MNIST_3':
        (train_X, train_Y), (valid_X, valid_Y), (test_X, test_Y) = data.load_mnist(state.data_path)
        train_X = numpy.concatenate((train_X, valid_X))
        train_Y = numpy.concatenate((train_Y, valid_Y))
        artificial = True
        try:
            dataset = int(state.dataset.split('_')[1])
        except:
            raise AssertionError("artificial dataset number not recognized. Input was "+state.dataset)
    else:
        raise AssertionError("dataset not recognized.")

    
    train_X = theano.shared(train_X)
    train_Y = theano.shared(train_Y)
    valid_X = theano.shared(valid_X)
    valid_Y = theano.shared(valid_Y) 
    test_X = theano.shared(test_X)
    test_Y = theano.shared(test_Y) 
   
    if artificial:
        print 'Sequencing MNIST data...'
        print 'train set size:',len(train_Y.eval())
        print 'valid set size:',len(valid_Y.eval())
        print 'test set size:',len(test_Y.eval())
        data.sequence_mnist_data(train_X, train_Y, valid_X, valid_Y, test_X, test_Y, dataset)
        print 'train set size:',len(train_Y.eval())
        print 'valid set size:',len(valid_Y.eval())
        print 'test set size:',len(test_Y.eval())
        print 'Sequencing done.'
        print
    
    N_input =   train_X.eval().shape[1]
    root_N_input = numpy.sqrt(N_input)
    
    # Network and training specifications
    layers          =   state.layers # number hidden layers
    walkbacks       =   state.walkbacks # number of walkbacks 
    layer_sizes     =   [N_input] + [state.hidden_size] * layers # layer sizes, from h0 to hK (h0 is the visible layer)
    learning_rate   =   theano.shared(cast32(state.learning_rate))  # learning rate
    recurrent_learning_rate = theano.shared(cast32(state.learning_rate))  # learning rate
    annealing       =   cast32(state.annealing) # exponential annealing coefficient
    momentum        =   theano.shared(cast32(state.momentum)) # momentum term 
    
    # Theano variables and RNG
    X       = T.fmatrix()
    X1      = T.fmatrix()
    MRG = RNG_MRG.MRG_RandomStreams(1)

    # PARAMETERS : weights list and bias list.
    # initialize a list of weights and biases based on layer_sizes
    weights_list    =   [get_shared_weights(layer_sizes[i], layer_sizes[i+1], name="W_{0!s}_{1!s}".format(i,i+1)) for i in range(layers)] # initialize each layer to uniform sample from sqrt(6. / (n_in + n_out))
    bias_list       =   [get_shared_bias(layer_sizes[i], name='b_'+str(i)) for i in range(layers + 1)] # initialize each layer to 0's.
    # parameters for recurrent part
    recurrent_weights_list    =   [get_shared_recurrent_weights(state.hidden_size, name="V_"+str(i+1)) for i in range(layers)] # initialize to identity matrix the size of hidden layer.
    recurrent_bias_list       =   [get_shared_bias(state.hidden_size, name='vb_'+str(i+1)) for i in range(layers)] # initialize to 0's.

#     if state.test_model:
#         # Load the parameters of the last epoch
#         # maybe if the path is given, load these specific attributes 
#         param_files     =   filter(lambda x:'params' in x, os.listdir('.'))
#         max_epoch_idx   =   numpy.argmax([int(x.split('_')[-1].split('.')[0]) for x in param_files])
#         params_to_load  =   param_files[max_epoch_idx]
#         PARAMS = cPickle.load(open(params_to_load,'r'))
#         [p.set_value(lp.get_value(borrow=False)) for lp, p in zip(PARAMS[:len(weights_list)], weights_list)]
#         [p.set_value(lp.get_value(borrow=False)) for lp, p in zip(PARAMS[len(weights_list):], bias_list)]
#         
#     if state.continue_training:
#         # Load the parameters of the last GSN
#         params_to_load = 'gsn_params.pkl'
#         PARAMS = cPickle.load(open(params_to_load,'r'))
#         [p.set_value(lp.get_value(borrow=False)) for lp, p in zip(PARAMS[:len(weights_list)], weights_list)]
#         [p.set_value(lp.get_value(borrow=False)) for lp, p in zip(PARAMS[len(weights_list):], bias_list)]
#         # Load the parameters of the last recurrent
# #         params_to_load = 'recurrent_params.pkl'
# #         PARAMS = cPickle.load(open(params_to_load,'r'))
# #         [p.set_value(lp.get_value(borrow=False)) for lp, p in zip(PARAMS[:len(weights_list)], weights_list)]
# #         [p.set_value(lp.get_value(borrow=False)) for lp, p in zip(PARAMS[len(weights_list):], bias_list)]

 
    ''' F PROP '''
    if state.act == 'sigmoid':
        print 'Using sigmoid activation for hiddens'
        hidden_activation = T.nnet.sigmoid
    elif state.act == 'rectifier':
        print 'Using rectifier activation for hiddens'
        hidden_activation = lambda x : T.maximum(cast32(0), x)
    elif state.act == 'tanh':
        print 'Using hyperbolic tangent activation for hiddens'
        hidden_activation = lambda x : T.tanh(x)
    
    print 'Using sigmoid activation for visible layer'
    visible_activation = T.nnet.sigmoid 
  
        
    def update_layers(hiddens, p_X_chain, noisy = True):
        print 'odd layer updates'
        update_odd_layers(hiddens, noisy)
        print 'even layer updates'
        update_even_layers(hiddens, p_X_chain, noisy)
        print 'done full update.'
        print
        
    def update_layers_reverse(hiddens, p_X_chain, noisy = True):
        print 'even layer updates'
        update_even_layers(hiddens, p_X_chain, noisy)
        print 'odd layer updates'
        update_odd_layers(hiddens, noisy)
        print 'done full update.'
        print
        
    # Odd layer update function
    # just a loop over the odd layers
    def update_odd_layers(hiddens, noisy):
        for i in range(1, len(hiddens), 2):
            print 'updating layer',i
            simple_update_layer(hiddens, None, i, add_noise = noisy)
    
    # Even layer update
    # p_X_chain is given to append the p(X|...) at each full update (one update = odd update + even update)
    def update_even_layers(hiddens, p_X_chain, noisy):
        for i in range(0, len(hiddens), 2):
            print 'updating layer',i
            simple_update_layer(hiddens, p_X_chain, i, add_noise = noisy)
    
    # The layer update function
    # hiddens   :   list containing the symbolic theano variables [visible, hidden1, hidden2, ...]
    #               layer_update will modify this list inplace
    # p_X_chain :   list containing the successive p(X|...) at each update
    #               update_layer will append to this list
    # add_noise     : pre and post activation gaussian noise
    
    def simple_update_layer(hiddens, p_X_chain, i, add_noise=True):   
        # Compute the dot product, whatever layer
        # If the visible layer X
        if i == 0:
            print 'using '+str(weights_list[i])+'.T'
            hiddens[i]  =   T.dot(hiddens[i+1], weights_list[i].T) + bias_list[i]           
        # If the top layer
        elif i == len(hiddens)-1:
            print 'using',weights_list[i-1]
            hiddens[i]  =   T.dot(hiddens[i-1], weights_list[i-1]) + bias_list[i]
        # Otherwise in-between layers
        else:
            print "using {0!s} and {1!s}.T".format(weights_list[i-1], weights_list[i])
            # next layer        :   hiddens[i+1], assigned weights : W_i
            # previous layer    :   hiddens[i-1], assigned weights : W_(i-1)
            hiddens[i]  =   T.dot(hiddens[i+1], weights_list[i].T) + T.dot(hiddens[i-1], weights_list[i-1]) + bias_list[i]
    
        # Add pre-activation noise if NOT input layer
        if i==1 and state.noiseless_h1:
            print '>>NO noise in first hidden layer'
            add_noise   =   False
    
        # pre activation noise            
        if i != 0 and add_noise:
            print 'Adding pre-activation gaussian noise for layer', i
            hiddens[i]  =   add_gaussian_noise(hiddens[i], state.hidden_add_noise_sigma)
       
        # ACTIVATION!
        if i == 0:
            print 'Sigmoid units activation for visible layer'
            hiddens[i]  =   visible_activation(hiddens[i])
        else:
            print 'Hidden units {} activation for layer'.format(state.act), i
            hiddens[i]  =   hidden_activation(hiddens[i])
    
        # post activation noise            
        if i != 0 and add_noise:
            print 'Adding post-activation gaussian noise for layer', i
            hiddens[i]  =   add_gaussian_noise(hiddens[i], state.hidden_add_noise_sigma)
    
        # build the reconstruction chain if updating the visible layer X
        if i == 0:
            # if input layer -> append p(X|...)
            p_X_chain.append(hiddens[i])
            
            # sample from p(X|...) - SAMPLING NEEDS TO BE CORRECT FOR INPUT TYPES I.E. FOR BINARY MNIST SAMPLING IS BINOMIAL. real-valued inputs should be gaussian
            if state.input_sampling:
                print 'Sampling from input'
                sampled     =   MRG.binomial(p = hiddens[i], size=hiddens[i].shape, dtype='float32')
            else:
                print '>>NO input sampling'
                sampled     =   hiddens[i]
            # add noise
            sampled     =   salt_and_pepper(sampled, state.input_salt_and_pepper)
            
            # set input layer
            hiddens[i]  =   sampled
                
    def perform_recurrent_step(hiddens):
        #odd layer predictions
        for i in range(len(hiddens)):
            if (i % 2) != 0: #if odd layer
                print 'using',recurrent_weights_list[i-1],'and',recurrent_bias_list[i-1]
                hiddens[i] = T.dot(hiddens[i],recurrent_weights_list[i-1]) + recurrent_bias_list[i-1]
                
    def build_graph(hiddens, p_X_chain, p_X1_chain, noiseflag):
        # The layer update scheme
        print "Building the graph :", walkbacks*2,"updates"
        for i in range(walkbacks):
            print "Walkback {!s}/{!s}".format(i+1,walkbacks)
            update_layers(hiddens, p_X_chain, noisy=noiseflag)
            
        perform_recurrent_step(hiddens) # do the regression!
         
        for i in range(walkbacks):
            print "Post Regression Walkback {!s}/{!s}".format(i+1,walkbacks)
            update_layers_reverse(hiddens, p_X1_chain, noisy=noiseflag)
        
        return hiddens, p_X_chain, p_X1_chain
            
        
   
    
    ''' Corrupt X '''
    X_corrupt   = salt_and_pepper(X, state.input_salt_and_pepper)

    ''' hidden layer init '''
    hiddens     = [X_corrupt]
    p_X_chain   = [] 
    p_X1_chain  = []
    noiseflag = True
    for w in weights_list:
        # init with zeros
        hiddens.append(T.zeros_like(T.dot(hiddens[-1], w)))

    hiddens, p_X_chain, p_X1_chain = build_graph(hiddens, p_X_chain, p_X1_chain, noiseflag)
        
        
    print "Creating functions for regression part"
    # The prediction of the recurrent regression - no noise! noise is only used as regularization for GSN from over-fitting
    hiddens1 = [X]
    recurrent_pX_chain  =   []
    recurrent_p_X1_chain  =   []
    noiseflag = False
    for w in weights_list:
        hiddens1.append(T.zeros_like(T.dot(hiddens1[-1], w)))
    
    hiddens1, recurrent_pX_chain, recurrent_p_X1_chain = build_graph(hiddens1, recurrent_pX_chain, recurrent_p_X1_chain, noiseflag)




    # COST AND GRADIENTS    
    print
    print 'Cost w.r.t p(X|...) at every step in the graph'
    COSTS            =   [T.mean(T.nnet.binary_crossentropy(rX, X)) for rX in p_X_chain]
    show_COST_pre    =   COSTS[-1]
    COST_pre         =   numpy.sum(COSTS)
    prediction_COSTS =   [T.mean(T.nnet.binary_crossentropy(rX1, X1)) for rX1 in p_X1_chain]
    #COST = [T.log(T.sum(T.pow((rX - X),2))) for rX in p_X_chain] #why does it work with log and not without???
    show_COST_post   =   prediction_COSTS[-1]
    COSTS            =   COSTS + prediction_COSTS
    COST             =   numpy.sum(COSTS)
    
    params      =   weights_list + bias_list    
    print "params:",params
    
    #l2 regularization
    recurrent_regularization_cost = T.sum([T.sum(recurrent_weights ** 2) for recurrent_weights in recurrent_weights_list])
    #recurrent_cost = T.log(T.sum(T.pow((predicted_network - encoded_network),2)) + recurrent_regularization_cost)
    recurrent_costs = [T.mean(T.nnet.binary_crossentropy(pX, X1)) for pX in recurrent_p_X1_chain]
    recurrent_cost_show = recurrent_costs[-1]
    recurrent_cost = numpy.sum(recurrent_costs) + state.regularize_weight * recurrent_regularization_cost
    
    #only using the odd layers update -> even-indexed parameters in the list because it starts at v1
    recurrent_params = [recurrent_weights_list[i] for i in range(len(recurrent_weights_list)) if i%2 == 0] + [recurrent_bias_list[i] for i in range(len(recurrent_bias_list)) if i%2 == 0]
    print "recurrent params:", recurrent_params    
    
    print "creating functions..."
    
    gradient_init        =   T.grad(COST_pre, params)
                
    gradient_buffer_init =   [theano.shared(numpy.zeros(param.get_value().shape, dtype='float32')) for param in params]
    
    m_gradient_init      =   [momentum * gb + (cast32(1) - momentum) * g for (gb, g) in zip(gradient_buffer_init, gradient_init)]
    param_updates_init   =   [(param, param - learning_rate * mg) for (param, mg) in zip(params, m_gradient_init)]
    gradient_buffer_updates_init = zip(gradient_buffer_init, m_gradient_init)
        
    updates_init         =   OrderedDict(param_updates_init + gradient_buffer_updates_init)
    
    
    
    gradient        =   T.grad(COST, params)
                
    gradient_buffer =   [theano.shared(numpy.zeros(param.get_value().shape, dtype='float32')) for param in params]
    
    m_gradient      =   [momentum * gb + (cast32(1) - momentum) * g for (gb, g) in zip(gradient_buffer, gradient)]
    param_updates   =   [(param, param - learning_rate * mg) for (param, mg) in zip(params, m_gradient)]
    gradient_buffer_updates = zip(gradient_buffer, m_gradient)
        
    updates         =   OrderedDict(param_updates + gradient_buffer_updates)
    
    
    
    f_cost          =   theano.function(inputs = [X, X1], outputs = [show_COST_pre, show_COST_post])


    f_learn     =   theano.function(inputs  = [X, X1], 
                                    updates = updates, 
                                    outputs = [show_COST_pre, show_COST_post])
    
    f_learn_init     =   theano.function(inputs  = [X], 
                                    updates = updates_init, 
                                    outputs = [show_COST_pre])
    
    
    recurrent_gradient        =   T.grad(recurrent_cost, recurrent_params)
    recurrent_gradient_buffer =   [theano.shared(numpy.zeros(param.get_value().shape, dtype='float32')) for param in recurrent_params]
    recurrent_m_gradient      =   [momentum * gb + (cast32(1) - momentum) * g for (gb, g) in zip(recurrent_gradient_buffer, recurrent_gradient)]
    recurrent_param_updates   =   [(param, param - recurrent_learning_rate * mg) for (param, mg) in zip(recurrent_params, recurrent_m_gradient)]
    recurrent_gradient_buffer_updates = zip(recurrent_gradient_buffer, recurrent_m_gradient)
        
    recurrent_updates         =   OrderedDict(recurrent_param_updates + recurrent_gradient_buffer_updates)
    
    recurrent_f_cost          =   theano.function(inputs = [X, X1], outputs = recurrent_cost_show)
        
    recurrent_f_learn         =   theano.function(inputs  = [X, X1], 
                                                  updates = recurrent_updates, 
                                                  outputs = recurrent_cost_show)

    
    print "functions done."
    print
    
    #############
    # Denoise some numbers  :   show number, noisy number, reconstructed number
    #############
    import random as R
    R.seed(1)
    # Grab 100 random indices from test_X
    random_idx      =   numpy.array(R.sample(range(len(test_X.get_value())), 100))
    numbers         =   test_X.get_value()[random_idx]
    
    f_noise         =   theano.function(inputs = [X], outputs = salt_and_pepper(X, state.input_salt_and_pepper))
    noisy_numbers   =   f_noise(test_X.get_value()[random_idx])
    #noisy_numbers   =   salt_and_pepper(numbers, state.input_salt_and_pepper)

    # Recompile the graph without noise for reconstruction function
    hiddens_R     = [X]
    p_X_chain_R   = []
    p_X1_chain_R   = []

    for w in weights_list:
        # init with zeros
        hiddens_R.append(T.zeros_like(T.dot(hiddens_R[-1], w)))

    # The layer update scheme
    print "Creating graph for noisy reconstruction function at checkpoints during training."
    hiddens_R, p_X_chain_R, p_X1_chain_R = build_graph(hiddens_R, p_X_chain_R, p_X1_chain_R, noiseflag=False) 

    f_recon = theano.function(inputs = [X], outputs = [p_X_chain_R[-1], p_X1_chain_R[-1]])
    f_walkbacks = theano.function(inputs = [X], outputs = p_X_chain_R + p_X1_chain_R) # function to get progression of walkbacks


    ############
    # Sampling #
    ############
    
    # the input to the sampling function
    network_state_input     =   [X] + [T.fmatrix() for i in range(layers)]
   
    # "Output" state of the network (noisy)
    # initialized with input, then we apply updates
    #network_state_output    =   network_state_input
    
    network_state_output    =   [X] + network_state_input[1:]

    visible_pX_chain        =   []

    # ONE update
    print "Performing one walkback in network state sampling."
    update_layers(network_state_output, visible_pX_chain, noisy=True)

    if layers == 1: 
        f_sample_simple = theano.function(inputs = [X], outputs = visible_pX_chain[-1])
    
    
    # WHY IS THERE A WARNING????
    # because the first odd layers are not used -> directly computed FROM THE EVEN layers
    # unused input = warn
    f_sample2   =   theano.function(inputs = network_state_input, outputs = network_state_output + visible_pX_chain, on_unused_input='warn')

    def sample_some_numbers_single_layer():
        x0    =   test_X.get_value()[7:8]
        samples = [x0]
        x  =   f_noise(x0)
        for i in range(399):
            x = f_sample_simple(x)
            samples.append(x)
            x = numpy.random.binomial(n=1, p=x, size=x.shape).astype('float32')
            x = f_noise(x)
        return numpy.vstack(samples)
            
    def sampling_wrapper(NSI):
        # * is the "splat" operator: It takes a list as input, and expands it into actual positional arguments in the function call.
        out             =   f_sample2(*NSI)
        NSO             =   out[:len(network_state_output)]
        vis_pX_chain    =   out[len(network_state_output):]
        return NSO, vis_pX_chain

    def sample_some_numbers(N=400):
        # The network's initial state
        init_vis        =   test_X.get_value()[7:8]

        noisy_init_vis  =   f_noise(init_vis)

        network_state   =   [[noisy_init_vis] + [numpy.zeros((1,len(b.get_value())), dtype='float32') for b in bias_list[1:]]]

        visible_chain   =   [init_vis]

        noisy_h0_chain  =   [noisy_init_vis]

        for i in range(N-1):
           
            # feed the last state into the network, compute new state, and obtain visible units expectation chain 
            net_state_out, vis_pX_chain =   sampling_wrapper(network_state[-1])

            # append to the visible chain
            visible_chain   +=  vis_pX_chain

            # append state output to the network state chain
            network_state.append(net_state_out)
            
            noisy_h0_chain.append(net_state_out[0])

        return numpy.vstack(visible_chain), numpy.vstack(noisy_h0_chain)
    
    def plot_samples(epoch_number, iteration_number):
        to_sample = time.time()
        if layers == 1:
            # one layer model
            V = sample_some_numbers_single_layer()
        else:
            V, _ = sample_some_numbers()
        img_samples =   PIL.Image.fromarray(tile_raster_images(V, (root_N_input,root_N_input), (20,20)))
        
        fname       =   outdir+'samples_iteration_'+str(iteration_number)+'_epoch_'+str(epoch_number)+'.png'
        img_samples.save(fname) 
        print 'Took ' + str(time.time() - to_sample) + ' to sample 400 numbers'
   
    ##############
    # Inpainting #
    ##############
    def inpainting(digit):
        # The network's initial state
        # NOISE INIT
        init_vis    =   cast32(numpy.random.uniform(size=digit.shape))

        #noisy_init_vis  =   f_noise(init_vis)
        #noisy_init_vis  =   cast32(numpy.random.uniform(size=init_vis.shape))

        # INDEXES FOR VISIBLE AND NOISY PART
        #noise_idx = (numpy.arange(N_input) % root_N_input < (root_N_input/2))
        fixed_idx = (numpy.arange(N_input) % root_N_input > (root_N_input/2))
        # function to re-init the visible to the same noise

        # FUNCTION TO RESET HALF VISIBLE TO DIGIT
        def reset_vis(V):
            V[0][fixed_idx] =   digit[0][fixed_idx]
            return V
        
        # INIT DIGIT : NOISE and RESET HALF TO DIGIT
        init_vis = reset_vis(init_vis)

        network_state   =   [[init_vis] + [numpy.zeros((1,len(b.get_value())), dtype='float32') for b in bias_list[1:]]]

        visible_chain   =   [init_vis]

        noisy_h0_chain  =   [init_vis]

        for i in range(49):
           
            # feed the last state into the network, compute new state, and obtain visible units expectation chain 
            net_state_out, vis_pX_chain =   sampling_wrapper(network_state[-1])


            # reset half the digit
            net_state_out[0] = reset_vis(net_state_out[0])
            vis_pX_chain[0]  = reset_vis(vis_pX_chain[0])

            # append to the visible chain
            visible_chain   +=  vis_pX_chain

            # append state output to the network state chain
            network_state.append(net_state_out)
            
            noisy_h0_chain.append(net_state_out[0])

        return numpy.vstack(visible_chain), numpy.vstack(noisy_h0_chain)

    def save_params(name, n, params, iteration_number):
        print 'saving parameters...'
        save_path = outdir+name+'_params_iteration_'+str(iteration_number)+'_epoch_'+str(n)+'.pkl'
        f = open(save_path, 'wb')
        try:
            cPickle.dump(params, f, protocol=cPickle.HIGHEST_PROTOCOL)
        finally:
            f.close() 


    ################
    # GSN TRAINING #
    ################
    def train_GSN(iteration_number, train_X, train_Y, valid_X, valid_Y, test_X, test_Y):
        print '----------------------------------------'
        print 'TRAINING GSN FOR ITERATION',iteration_number
        with open(logfile,'a') as f:
            f.write("--------------------------\nTRAINING GSN FOR ITERATION {0!s}\n".format(iteration_number))
        
        # TRAINING
        n_epoch     =   state.n_epoch
        batch_size  =   state.batch_size
        STOP        =   False
        counter     =   0
        if iteration_number == 0:
            learning_rate.set_value(cast32(state.learning_rate))  # learning rate
        times = []
        best_cost = float('inf')
        patience = 0
            
        print 'learning rate:',learning_rate.get_value()
        
        print 'train X size:',str(train_X.shape.eval())
        print 'valid X size:',str(valid_X.shape.eval())
        print 'test X size:',str(test_X.shape.eval())
    
        pre_train_costs =   []
        pre_valid_costs =   []
        pre_test_costs  =   []
        post_train_costs =   []
        post_valid_costs =   []
        post_test_costs  =   []
        
        if state.vis_init:
            bias_list[0].set_value(logit(numpy.clip(0.9,0.001,train_X.get_value().mean(axis=0))))
    
        if state.test_model:
            # If testing, do not train and go directly to generating samples, parzen window estimation, and inpainting
            print 'Testing : skip training'
            STOP    =   True
    
    
        while not STOP:
            counter += 1
            t = time.time()
            print counter,'\t',
            with open(logfile,'a') as f:
                f.write("{0!s}\t".format(counter))
                
            #shuffle the data
            data.sequence_mnist_data(train_X, train_Y, valid_X, valid_Y, test_X, test_Y, dataset)
                
            #train
            pre_train_cost = []
            post_train_cost = []
            if iteration_number == 0: # if first run through (recurrent_weights V is the identity matrix), can do batch
                for i in range(len(train_X.get_value(borrow=True)) / batch_size):
                    x = train_X.get_value()[i * batch_size : (i+1) * batch_size]
                    pre = f_learn_init(x)
                    pre_train_cost.append(pre)
                    post_train_cost.append(-1)
            else: # otherwise, no batch
                for i in range(len(train_X.get_value(borrow=True)) / batch_size):
                    x = train_X.get_value()[i * batch_size : (i+1) * batch_size]
                    x1 = train_X.get_value()[(i * batch_size) + 1 : ((i+1) * batch_size) + 1]
                    x,x1 = fix_input_size(x,x1)
                    pre, post = f_learn(x,x1)
                    pre_train_cost.append(pre)
                    post_train_cost.append(post)
                
            pre_train_cost = numpy.mean(pre_train_cost) 
            pre_train_costs.append(pre_train_cost)
            post_train_cost = numpy.mean(post_train_cost) 
            post_train_costs.append(post_train_cost)
            print 'Train : ',trunc(pre_train_cost),trunc(post_train_cost), '\t',
            with open(logfile,'a') as f:
                f.write("Train : {0!s} {1!s}\t".format(trunc(pre_train_cost),trunc(post_train_cost)))
            with open(train_convergence_pre,'a') as f:
                f.write("{0!s},".format(pre_train_cost))
            with open(train_convergence_post,'a') as f:
                f.write("{0!s},".format(post_train_cost))
    
            #valid
            pre_valid_cost  =   []    
            post_valid_cost  =  []
            if iteration_number == 0:
                for i in range(len(valid_X.get_value(borrow=True)) / batch_size):
                    x = valid_X.get_value()[i * batch_size : (i+1) * batch_size]
                    pre, post = f_cost(x,x)
                    pre_valid_cost.append(pre)
                    post_valid_cost.append(post)
            else:
                for i in range(len(valid_X.get_value(borrow=True)) / batch_size):
                    x = valid_X.get_value()[i * batch_size : (i+1) * batch_size]
                    x1 = valid_X.get_value()[(i * batch_size) + 1 : ((i+1) * batch_size) + 1]
                    x,x1 = fix_input_size(x,x1)
                    pre, post = f_cost(x, x1)
                    pre_valid_cost.append(pre)
                    post_valid_cost.append(post)
                    
            pre_valid_cost = numpy.mean(pre_valid_cost) 
            pre_valid_costs.append(pre_valid_cost)
            post_valid_cost = numpy.mean(post_valid_cost) 
            post_valid_costs.append(post_valid_cost)
            print 'Valid : ', trunc(pre_valid_cost),trunc(post_valid_cost), '\t',
            with open(logfile,'a') as f:
                f.write("Valid : {0!s} {1!s}\t".format(trunc(pre_valid_cost),trunc(post_valid_cost)))
            with open(valid_convergence_pre,'a') as f:
                f.write("{0!s},".format(pre_valid_cost))
            with open(valid_convergence_post,'a') as f:
                f.write("{0!s},".format(post_valid_cost))
    
            #test
            pre_test_cost  =   []
            post_test_cost  =   []
            if iteration_number == 0:
                for i in range(len(test_X.get_value(borrow=True)) / batch_size):
                    x = test_X.get_value()[i * batch_size : (i+1) * batch_size]
                    pre, post = f_cost(x,x)
                    pre_test_cost.append(pre)
                    post_test_cost.append(post)
            else:
                for i in range(len(test_X.get_value(borrow=True)) / batch_size):
                    x = test_X.get_value()[i * batch_size : (i+1) * batch_size]
                    x1 = test_X.get_value()[(i * batch_size) + 1 : ((i+1) * batch_size) + 1]
                    x,x1 = fix_input_size(x,x1)
                    pre, post = f_cost(x, x1)
                    pre_test_cost.append(pre)
                    post_test_cost.append(post)
                
            pre_test_cost = numpy.mean(pre_test_cost) 
            pre_test_costs.append(pre_test_cost)
            post_test_cost = numpy.mean(post_test_cost) 
            post_test_costs.append(post_test_cost)
            print 'Test  : ', trunc(pre_test_cost),trunc(post_test_cost), '\t',
            with open(logfile,'a') as f:
                f.write("Test : {0!s} {1!s}\t".format(trunc(pre_test_cost),trunc(post_test_cost)))
            with open(test_convergence_pre,'a') as f:
                f.write("{0!s},".format(pre_test_cost))
            with open(test_convergence_post,'a') as f:
                f.write("{0!s},".format(post_test_cost))
                
            #check for early stopping
            cost = pre_train_cost
            if iteration_number != 0:
                cost = cost + post_train_cost
            if cost < best_cost*state.early_stop_threshold:
                patience = 0
                best_cost = cost
            else:
                patience += 1
    
            if counter >= n_epoch or patience >= state.early_stop_length:
                STOP = True
                save_params('gsn', counter, params, iteration_number)
    
            timing = time.time() - t
            times.append(timing)
    
            print 'time : ', trunc(timing),
            
            print 'remaining: ', trunc((n_epoch - counter) * numpy.mean(times) / 60 / 60), 'hrs'
    
            with open(logfile,'a') as f:
                f.write("MeanVisB : {0!s}\t".format(trunc(bias_list[0].get_value().mean())))
            
            with open(logfile,'a') as f:
                f.write("W : {0!s}\t".format(str([trunc(abs(w.get_value(borrow=True)).mean()) for w in weights_list])))
                
            with open(logfile,'a') as f:
                f.write("Time : {0!s} seconds\n".format(trunc(timing)))
    
            if (counter % state.save_frequency) == 0:
                # Checking reconstruction
                reconstructed, reconstructed_prediction   =   f_recon(noisy_numbers) 
                # Concatenate stuff
                stacked = numpy.vstack([numpy.vstack([numbers[i*10 : (i+1)*10], noisy_numbers[i*10 : (i+1)*10], reconstructed[i*10 : (i+1)*10], reconstructed_prediction[i*10 : (i+1)*10]]) for i in range(10)])
            
                number_reconstruction   =   PIL.Image.fromarray(tile_raster_images(stacked, (root_N_input,root_N_input), (10,40)))
                #epoch_number    =   reduce(lambda x,y : x + y, ['_'] * (4-len(str(counter)))) + str(counter)
                number_reconstruction.save(outdir+'gsn_number_reconstruction_iteration_'+str(iteration_number)+'_epoch_'+str(counter)+'.png')
        
                #sample_numbers(counter, 'seven')
                plot_samples(counter, iteration_number)
        
                #save params
                save_params('gsn', counter, params, iteration_number)
         
            # ANNEAL!
            new_lr = learning_rate.get_value() * annealing
            learning_rate.set_value(new_lr)
    
        # Save
        state.pre_train_costs = pre_train_costs
        state.post_train_costs = post_train_costs
        state.pre_valid_costs = pre_valid_costs
        state.post_valid_costs = post_valid_costs
        state.pre_test_costs = pre_test_costs
        state.post_test_costs = post_test_costs
    
        # if test
    
        # 10k samples
        print 'Generating 10,000 samples'
        samples, _  =   sample_some_numbers(N=10000)
        f_samples   =   outdir+'samples.npy'
        numpy.save(f_samples, samples)
        print 'saved digits'
    
    
        # parzen
#         print 'Evaluating parzen window'
#         import likelihood_estimation_parzen
#         likelihood_estimation_parzen.main(0.20,'mnist') 
    
        # Inpainting
        '''
        print 'Inpainting'
        test_X  =   test_X.get_value()
    
        numpy.random.seed(2)
        test_idx    =   numpy.arange(len(test_Y.get_value(borrow=True)))
    
        for Iter in range(10):
    
            numpy.random.shuffle(test_idx)
            test_X = test_X[test_idx]
            test_Y = test_Y[test_idx]
    
            digit_idx = [(test_Y==i).argmax() for i in range(10)]
            inpaint_list = []
    
            for idx in digit_idx:
                DIGIT = test_X[idx:idx+1]
                V_inpaint, H_inpaint = inpainting(DIGIT)
                inpaint_list.append(V_inpaint)
    
            INPAINTING  =   numpy.vstack(inpaint_list)
    
            plot_inpainting =   PIL.Image.fromarray(tile_raster_images(INPAINTING, (root_N_input,root_N_input), (10,50)))
    
            fname   =   'inpainting_'+str(Iter)+'_iteration_'+str(iteration_number)+'.png'
            #fname   =   os.path.join(state.model_path, fname)
    
            plot_inpainting.save(fname)
    '''        
            
            
            
            
    def train_recurrent(iteration_number, train_X, train_Y, valid_X, valid_Y, test_X, test_Y):
        print '-------------------------------------------'
        print 'TRAINING RECURRENT REGRESSION FOR ITERATION',iteration_number
        with open(logfile,'a') as f:
            f.write("\n\n--------------------------\nTRAINING RECURRENT REGRESSION FOR ITERATION {0!s}\n".format(iteration_number))
        
        # TRAINING
        n_epoch     =   state.n_epoch
        batch_size  =   state.batch_size
        STOP        =   False
        counter     =   0
        best_cost = float('inf')
        patience = 0
        if iteration_number == 0:
            recurrent_learning_rate.set_value(cast32(state.learning_rate))  # learning rate
        times = []
            
        print 'learning rate:',recurrent_learning_rate.get_value()
        
        print 'train X size:',str(train_X.shape.eval())
        print 'valid X size:',str(valid_X.shape.eval())
        print 'test X size:',str(test_X.shape.eval())
    
        train_costs =   []
        valid_costs =   []
        test_costs  =   []
    
        if state.test_model:
            # If testing, do not train and go directly to generating samples, parzen window estimation, and inpainting
            print 'Testing : skip training'
            STOP    =   True

        while not STOP:
            counter += 1
            t = time.time()
            print counter,'\t',
            with open(logfile,'a') as f:
                f.write("{0!s}\t".format(counter))
                
            #shuffle the data
            data.sequence_mnist_data(train_X, train_Y, valid_X, valid_Y, test_X, test_Y, dataset)
    
            #train
            train_cost = []
            for i in range(len(train_X.get_value(borrow=True)) / batch_size):
                x = train_X.get_value()[i * batch_size : (i+1) * batch_size]
                x1 = train_X.get_value()[(i * batch_size) + 1 : ((i+1) * batch_size) + 1]
                x,x1 = fix_input_size(x,x1)
                train_cost.append(recurrent_f_learn(x, x1))
                
            train_cost = numpy.mean(train_cost) 
            train_costs.append(train_cost)
            print 'rTrain : ',trunc(train_cost), '\t',
            with open(logfile,'a') as f:
                f.write("rTrain : {0!s}\t".format(trunc(train_cost)))
            with open(recurrent_train_convergence,'a') as f:
                f.write("{0!s},".format(train_cost))
    
            #valid
            valid_cost  =   []
            for i in range(len(valid_X.get_value(borrow=True)) / batch_size):
                x = valid_X.get_value()[i * batch_size : (i+1) * batch_size]
                x1 = valid_X.get_value()[(i * batch_size) + 1 : ((i+1) * batch_size) + 1]
                x,x1 = fix_input_size(x,x1)
                valid_cost.append(recurrent_f_cost(x, x1))
            valid_cost = numpy.mean(valid_cost)
            valid_costs.append(valid_cost)
            print 'rValid : ', trunc(valid_cost), '\t',
            with open(logfile,'a') as f:
                f.write("rValid : {0!s}\t".format(trunc(valid_cost)))
            with open(recurrent_valid_convergence,'a') as f:
                f.write("{0!s},".format(valid_cost))
    
            #test
            test_cost  =   []
            for i in range(len(test_X.get_value(borrow=True)) / batch_size):
                x = test_X.get_value()[i * batch_size : (i+1) * batch_size]
                x1 = test_X.get_value()[(i * batch_size) + 1 : ((i+1) * batch_size) + 1]
                x,x1 = fix_input_size(x,x1)
                test_cost.append(recurrent_f_cost(x, x1))
            test_cost = numpy.mean(test_cost)
            test_costs.append(test_cost)
            print 'rTest  : ', trunc(test_cost), '\t',
            with open(logfile,'a') as f:
                f.write("rTest : {0!s}\t".format(trunc(test_cost)))
            with open(recurrent_test_convergence,'a') as f:
                f.write("{0!s},".format(test_cost))
    
            #check for early stopping
            cost = train_cost
            if cost < best_cost*state.early_stop_threshold:
                patience = 0
                best_cost = cost
            else:
                patience += 1
                
            if counter >= n_epoch or patience >= state.early_stop_length:
                STOP = True
                save_params('recurrent', counter, recurrent_params, iteration_number)
    
            timing = time.time() - t
            times.append(timing)
    
            print 'time : ', trunc(timing),
            
            print 'remaining: ', trunc((n_epoch - counter) * numpy.mean(times) / 60 / 60), 'hrs'
                            
            with open(logfile,'a') as f:
                f.write("MeanVB : {0!s}\t".format(str([trunc(vb.get_value().mean()) for vb in recurrent_bias_list])))
            
            with open(logfile,'a') as f:
                f.write("V : {0!s}\t".format(str([trunc(abs(v.get_value(borrow=True)).mean()) for v in recurrent_weights_list])))
                
            with open(logfile,'a') as f:
                f.write("Time : {0!s} seconds\n".format(trunc(timing)))
    
            if (counter % state.save_frequency) == 0: 
                # Checking reconstruction
                reconstructed, reconstructed_prediction   =   f_recon(noisy_numbers) 
                # Concatenate stuff
                stacked = numpy.vstack([numpy.vstack([numbers[i*10 : (i+1)*10], noisy_numbers[i*10 : (i+1)*10], reconstructed[i*10 : (i+1)*10], reconstructed_prediction[i*10 : (i+1)*10]]) for i in range(10)])
            
                number_reconstruction   =   PIL.Image.fromarray(tile_raster_images(stacked, (root_N_input,root_N_input), (10,40)))
                #epoch_number    =   reduce(lambda x,y : x + y, ['_'] * (4-len(str(counter)))) + str(counter)
                number_reconstruction.save(outdir+'recurrent_number_reconstruction_iteration_'+str(iteration_number)+'_epoch_'+str(counter)+'.png')
             
                #save params
                save_params('recurrent', counter, recurrent_params, iteration_number)
         
            # ANNEAL!
            new_r_lr = recurrent_learning_rate.get_value() * annealing
            recurrent_learning_rate.set_value(new_r_lr)
    
        # Save
        state.recurrent_train_costs = train_costs
        state.recurrent_valid_costs = valid_costs
        state.recurrent_test_costs = test_costs
            
            
            
            
    #####################
    # STORY 1 ALGORITHM #
    #####################
    
    for _ in range(state.max_iterations):
        train_GSN(iter, train_X, train_Y, valid_X, valid_Y, test_X, test_Y)
        train_recurrent(iter, train_X, train_Y, valid_X, valid_Y, test_X, test_Y)
        
          
        
        
