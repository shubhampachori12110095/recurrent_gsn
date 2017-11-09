import time
import warnings
from collections import OrderedDict

import PIL.Image
import cPickle
import numpy
import numpy.random as rng
import os
import theano
import theano.sandbox.rng_mrg as RNG_MRG
import theano.tensor as T
from utils import data_tools as data
from utils.image_tiler import tile_raster_images
from utils.utils import *


def experiment(state, outdir_base='./'):
    rng.seed(1)  # seed the numpy random generator
    # Initialize output directory and files
    data.mkdir_p(outdir_base)
    outdir = outdir_base + "/" + state.dataset + "/"
    data.mkdir_p(outdir)
    logfile = outdir + "log.txt"
    with open(logfile, 'w') as f:
        f.write("MODEL 2, {0!s}\n\n".format(state.dataset))
    train_convergence_pre = outdir + "train_convergence_pre.csv"
    train_convergence_post = outdir + "train_convergence_post.csv"
    valid_convergence_pre = outdir + "valid_convergence_pre.csv"
    valid_convergence_post = outdir + "valid_convergence_post.csv"
    test_convergence_pre = outdir + "test_convergence_pre.csv"
    test_convergence_post = outdir + "test_convergence_post.csv"

    print
    print
    "----------MODEL 2, {0!s}--------------".format(state.dataset)
    print

    # load parameters from config file if this is a test
    config_filename = outdir + 'config'
    if state.test_model and 'config' in os.listdir(outdir):
        config_vals = load_from_config(config_filename)
        for CV in config_vals:
            print
            CV
            if CV.startswith('test'):
                print
                'Do not override testing switch'
                continue
            try:
                exec('state.' + CV) in globals(), locals()
            except:
                exec('state.' + CV.split('=')[0] + "='" + CV.split('=')[1] + "'") in globals(), locals()
    else:
        # Save the current configuration
        # Useful for logs/experiments
        print
        'Saving config'
        with open(config_filename, 'w') as f:
            f.write(str(state))

    print
    state
    # Load the data, train = train+valid, and sequence
    artificial = False
    if state.dataset == 'MNIST_1' or state.dataset == 'MNIST_2' or state.dataset == 'MNIST_3':
        (train_X, train_Y), (valid_X, valid_Y), (test_X, test_Y) = data.load_mnist(state.data_path)
        train_X = numpy.concatenate((train_X, valid_X))
        train_Y = numpy.concatenate((train_Y, valid_Y))
        artificial = True
        try:
            dataset = int(state.dataset.split('_')[1])
        except:
            raise AssertionError("artificial dataset number not recognized. Input was " + state.dataset)
    else:
        raise AssertionError("dataset not recognized.")

    train_X = theano.shared(train_X)
    train_Y = theano.shared(train_Y)
    valid_X = theano.shared(valid_X)
    valid_Y = theano.shared(valid_Y)
    test_X = theano.shared(test_X)
    test_Y = theano.shared(test_Y)

    if artificial:
        print
        'Sequencing MNIST data...'
        print
        'train set size:', len(train_Y.eval())
        print
        'valid set size:', len(valid_Y.eval())
        print
        'test set size:', len(test_Y.eval())
        data.sequence_mnist_data(train_X, train_Y, valid_X, valid_Y, test_X, test_Y, dataset, rng)
        print
        'train set size:', len(train_Y.eval())
        print
        'valid set size:', len(valid_Y.eval())
        print
        'test set size:', len(test_Y.eval())
        print
        'Sequencing done.'
        print

    N_input = train_X.eval().shape[1]
    root_N_input = numpy.sqrt(N_input)

    # Network and training specifications
    layers = state.layers  # number hidden layers
    walkbacks = state.walkbacks  # number of walkbacks
    layer_sizes = [N_input] + [state.hidden_size] * layers  # layer sizes, from h0 to hK (h0 is the visible layer)
    learning_rate = theano.shared(cast32(state.learning_rate))  # learning rate
    annealing = cast32(state.annealing)  # exponential annealing coefficient
    momentum = theano.shared(cast32(state.momentum))  # momentum term

    # PARAMETERS : weights list and bias list.
    # initialize a list of weights and biases based on layer_sizes
    weights_list = [get_shared_weights(layer_sizes[i], layer_sizes[i + 1], name="W_{0!s}_{1!s}".format(i, i + 1)) for i
                    in range(layers)]  # initialize each layer to uniform sample from sqrt(6. / (n_in + n_out))
    recurrent_weights_list = [
        get_shared_weights(layer_sizes[i + 1], layer_sizes[i], name="V_{0!s}_{1!s}".format(i + 1, i)) for i in
        range(layers)]  # initialize each layer to uniform sample from sqrt(6. / (n_in + n_out))
    bias_list = [get_shared_bias(layer_sizes[i], name='b_' + str(i)) for i in
                 range(layers + 1)]  # initialize each layer to 0's.

    # Theano variables and RNG
    MRG = RNG_MRG.MRG_RandomStreams(1)
    X = T.fmatrix('X')
    Xs = [T.fmatrix(name="X_initial") if i == 0 else T.fmatrix(name="X_" + str(i + 1)) for i in range(walkbacks + 1)]
    hiddens_input = [X] + [T.fmatrix(name="h_" + str(i + 1)) for i in range(layers)]
    hiddens_output = hiddens_input[:1] + hiddens_input[1:]

    # Check variables for bad inputs and stuff
    if state.batch_size > len(Xs):
        warnings.warn(
            "Batch size should not be bigger than walkbacks+1 (len(Xs)) unless you know what you're doing. You need to know the sequence length beforehand.")
    if state.batch_size <= 0:
        raise AssertionError("batch size cannot be <= 0")

    ''' F PROP '''
    if state.hidden_act == 'sigmoid':
        print
        'Using sigmoid activation for hiddens'
        hidden_activation = T.nnet.sigmoid
    elif state.hidden_act == 'rectifier':
        print
        'Using rectifier activation for hiddens'
        hidden_activation = lambda x: T.maximum(cast32(0), x)
    elif state.hidden_act == 'tanh':
        print
        'Using hyperbolic tangent activation for hiddens'
        hidden_activation = lambda x: T.tanh(x)
    else:
        raise AssertionError("Did not recognize hidden activation {0!s}, please use tanh, rectifier, or sigmoid".format(
            state.hidden_act))

    if state.visible_act == 'sigmoid':
        print
        'Using sigmoid activation for visible layer'
        visible_activation = T.nnet.sigmoid
    elif state.visible_act == 'softmax':
        print
        'Using softmax activation for visible layer'
        visible_activation = T.nnet.softmax
    else:
        raise AssertionError(
            "Did not recognize visible activation {0!s}, please use sigmoid or softmax".format(state.visible_act))

    def update_layers(hiddens, p_X_chain, Xs, sequence_idx, noisy=True, sampling=True):
        print
        'odd layer updates'
        update_odd_layers(hiddens, noisy)
        print
        'even layer updates'
        update_even_layers(hiddens, p_X_chain, Xs, sequence_idx, noisy, sampling)
        # choose the correct output for hidden_outputs based on batch_size and walkbacks (this is due to an issue with batches, see note in run_story2.py)
        if state.batch_size <= len(Xs) and sequence_idx == state.batch_size - 1:
            return hiddens
        else:
            return None
        print
        'done full update.'
        print

    # Odd layer update function
    # just a loop over the odd layers
    def update_odd_layers(hiddens, noisy):
        for i in range(1, len(hiddens), 2):
            print
            'updating layer', i
            simple_update_layer(hiddens, None, None, None, i, add_noise=noisy)

    # Even layer update
    # p_X_chain is given to append the p(X|...) at each full update (one update = odd update + even update)
    def update_even_layers(hiddens, p_X_chain, Xs, sequence_idx, noisy, sampling):
        for i in range(0, len(hiddens), 2):
            print
            'updating layer', i
            simple_update_layer(hiddens, p_X_chain, Xs, sequence_idx, i, add_noise=noisy, input_sampling=sampling)

    # The layer update function
    # hiddens   :   list containing the symbolic theano variables [visible, hidden1, hidden2, ...]
    #               layer_update will modify this list inplace
    # p_X_chain :   list containing the successive p(X|...) at each update
    #               update_layer will append to this list
    # add_noise     : pre and post activation gaussian noise

    def simple_update_layer(hiddens, p_X_chain, Xs, sequence_idx, i, add_noise=True, input_sampling=True):
        # Compute the dot product, whatever layer
        # If the visible layer X
        if i == 0:
            print
            'using', recurrent_weights_list[i]
            hiddens[i] = (T.dot(hiddens[i + 1], recurrent_weights_list[i]) + bias_list[i])
        # If the top layer
        elif i == len(hiddens) - 1:
            print
            'using', weights_list[i - 1]
            hiddens[i] = T.dot(hiddens[i - 1], weights_list[i - 1]) + bias_list[i]
        # Otherwise in-between layers
        else:
            # next layer        :   hiddens[i+1], assigned weights : W_i
            # previous layer    :   hiddens[i-1], assigned weights : W_(i-1)
            print
            "using {0!s} and {1!s}".format(weights_list[i - 1], recurrent_weights_list[i])
            hiddens[i] = T.dot(hiddens[i + 1], recurrent_weights_list[i]) + T.dot(hiddens[i - 1], weights_list[i - 1]) + \
                         bias_list[i]

        # Add pre-activation noise if NOT input layer
        if i == 1 and state.noiseless_h1:
            print
            '>>NO noise in first hidden layer'
            add_noise = False

        # pre activation noise            
        if i != 0 and add_noise:
            print
            'Adding pre-activation gaussian noise for layer', i
            hiddens[i] = add_gaussian_noise(hiddens[i], state.hidden_add_noise_sigma)

        # ACTIVATION!
        if i == 0:
            print
            'Sigmoid units activation for visible layer X'
            hiddens[i] = visible_activation(hiddens[i])
        else:
            print
            'Hidden units {} activation for layer'.format(state.act), i
            hiddens[i] = hidden_activation(hiddens[i])

            # post activation noise
            # why is there post activation noise? Because there is already pre-activation noise, this just doubles the amount of noise between each activation of the hiddens.
        #         if i != 0 and add_noise:
        #             print 'Adding post-activation gaussian noise for layer', i
        #             hiddens[i]  =   add_gaussian(hiddens[i], state.hidden_add_noise_sigma)

        # build the reconstruction chain if updating the visible layer X
        if i == 0:
            # if input layer -> append p(X|...)
            p_X_chain.append(hiddens[i])  # what the predicted next input should be

            if sequence_idx + 1 < len(Xs):
                next_input = Xs[sequence_idx + 1]
                # sample from p(X|...) - SAMPLING NEEDS TO BE CORRECT FOR INPUT TYPES I.E. FOR BINARY MNIST SAMPLING IS BINOMIAL. real-valued inputs should be gaussian
                if input_sampling:
                    print
                    'Sampling from input'
                    sampled = MRG.binomial(p=next_input, size=next_input.shape, dtype='float32')
                else:
                    print
                    '>>NO input sampling'
                    sampled = next_input
                # add noise
                sampled = salt_and_pepper(sampled, state.input_salt_and_pepper)

                # DOES INPUT SAMPLING MAKE SENSE FOR SEQUENTIAL? - not really since it was used in walkbacks which was gibbs.
                # set input layer
                hiddens[i] = sampled

    def build_graph(hiddens, Xs, noisy=True, sampling=True):
        predicted_X_chain = []  # the visible layer that gets generated at each update_layers run
        H_chain = []  # either None or hiddens that gets generated at each update_layers run, this is used to determine what the correct hiddens_output should be
        print
        "Building the graph :", walkbacks, "updates"
        for i in range(walkbacks):
            print
            "Forward Prediction {!s}/{!s}".format(i + 1, walkbacks)
            H_chain.append(update_layers(hiddens, predicted_X_chain, Xs, i, noisy, sampling))
        return predicted_X_chain, H_chain

    '''Build the main training graph'''
    # corrupt x
    hiddens_output[0] = salt_and_pepper(hiddens_output[0], state.input_salt_and_pepper)
    # build the computation graph and the generated visible layers and appropriate hidden_output
    predicted_X_chain, H_chain = build_graph(hiddens_output, Xs, noisy=True, sampling=state.input_sampling)
    #     predicted_X_chain, H_chain = build_graph(hiddens_output, Xs, noisy=False, sampling=state.input_sampling) #testing one-hot without noise


    # choose the correct output for hiddens_output (this is due to the issue with batches - see note in run_story2.py)
    # this finds the not-None element of H_chain and uses that for hiddens_output
    h_empty = [True if h is None else False for h in H_chain]
    if False in h_empty:  # if there was a not-None element
        hiddens_output = H_chain[h_empty.index(False)]  # set hiddens_output to the appropriate element from H_chain

    ######################
    # COST AND GRADIENTS #
    ######################
    print
    if state.cost_funct == 'binary_crossentropy':
        print
        'Using binary cross-entropy cost!'
        cost_function = lambda x, y: T.mean(T.nnet.binary_crossentropy(x, y))
    elif state.cost_funct == 'square':
        print
        "Using square error cost!"
        cost_function = lambda x, y: T.mean(T.sqr(x - y))
    else:
        raise AssertionError(
            "Did not recognize cost function {0!s}, please use binary_crossentropy or square".format(state.cost_funct))
    print
    'Cost w.r.t p(X|...) at every step in the graph'

    costs = [cost_function(predicted_X_chain[i], Xs[i + 1]) for i in range(len(predicted_X_chain))]
    # outputs for the functions
    show_COSTs = [costs[0]] + [costs[-1]]

    # cost for the gradient
    # care more about the immediate next predictions rather than the future - use exponential decay
    #     COST = T.sum(costs)
    COST = T.sum([T.exp(-i / T.ceil(walkbacks / 3)) * costs[i] for i in range(len(costs))])

    params = weights_list + recurrent_weights_list + bias_list
    print
    "params:", params

    print
    "creating functions..."
    gradient = T.grad(COST, params)

    gradient_buffer = [theano.shared(numpy.zeros(param.get_value().shape, dtype='float32')) for param in params]

    m_gradient = [momentum * gb + (cast32(1) - momentum) * g for (gb, g) in zip(gradient_buffer, gradient)]
    param_updates = [(param, param - learning_rate * mg) for (param, mg) in zip(params, m_gradient)]
    gradient_buffer_updates = zip(gradient_buffer, m_gradient)

    updates = OrderedDict(param_updates + gradient_buffer_updates)

    # odd layer h's not used from input -> calculated directly from even layers (starting with h_0) since the odd layers are updated first.
    f_cost = theano.function(inputs=hiddens_input + Xs,
                             outputs=hiddens_output + show_COSTs,
                             on_unused_input='warn')

    f_learn = theano.function(inputs=hiddens_input + Xs,
                              updates=updates,
                              outputs=hiddens_output + show_COSTs,
                              on_unused_input='warn')

    print
    "functions done."
    print

    #############
    # Denoise some numbers  :   show number, noisy number, reconstructed number
    #############
    import random as R
    R.seed(1)
    # a function to add salt and pepper noise
    f_noise = theano.function(inputs=[X], outputs=salt_and_pepper(X, state.input_salt_and_pepper))

    # Recompile the graph without noise for reconstruction function - the input x_recon is already going to be noisy, and this is to test on a simulated 'real' input.
    X_recon = T.fvector("X_recon")
    Xs_recon = [T.fvector("Xs_recon")]
    hiddens_R_input = [X_recon] + [T.fvector(name="h_recon_" + str(i + 1)) for i in range(layers)]
    hiddens_R_output = hiddens_R_input[:1] + hiddens_R_input[1:]

    # The layer update scheme
    print
    "Creating graph for noisy reconstruction function at checkpoints during training."
    p_X_chain_R, H_chain_R = build_graph(hiddens_R_output, Xs_recon, noisy=False)

    # choose the correct output from H_chain for hidden_outputs based on batch_size and walkbacks
    # choose the correct output for hiddens_output
    h_empty = [True if h is None else False for h in H_chain_R]
    if False in h_empty:  # if there was a set of hiddens output from the batch_size-1 element of the chain
        hiddens_R_output = H_chain_R[
            h_empty.index(False)]  # extract out the not-None element from the list if it exists
    #     if state.batch_size <= len(Xs_recon):
    #         for i in range(len(hiddens_R_output)):
    #             hiddens_R_output[i] = H_chain_R[state.batch_size - 1][i]

    f_recon = theano.function(inputs=hiddens_R_input + Xs_recon,
                              outputs=hiddens_R_output + [p_X_chain_R[0], p_X_chain_R[-1]],
                              on_unused_input="warn")

    ############
    # Sampling #
    ############

    # the input to the sampling function
    X_sample = T.fmatrix("X_sampling")
    network_state_input = [X_sample] + [T.fmatrix("H_sampling_" + str(i + 1)) for i in range(layers)]

    # "Output" state of the network (noisy)
    # initialized with input, then we apply updates

    network_state_output = [X_sample] + network_state_input[1:]

    visible_pX_chain = []

    # ONE update
    print
    "Performing one walkback in network state sampling."
    _ = update_layers(network_state_output, visible_pX_chain, [X_sample], 0, noisy=True)

    if layers == 1:
        f_sample_simple = theano.function(inputs=[X_sample], outputs=visible_pX_chain[-1])

    # WHY IS THERE A WARNING????
    # because the first odd layers are not used -> directly computed FROM THE EVEN layers
    # unused input = warn
    f_sample2 = theano.function(inputs=network_state_input, outputs=network_state_output + visible_pX_chain,
                                on_unused_input='warn')

    def sample_some_numbers_single_layer():
        x0 = test_X.get_value()[:1]
        samples = [x0]
        x = f_noise(x0)
        for i in range(399):
            x = f_sample_simple(x)
            samples.append(x)
            x = numpy.random.binomial(n=1, p=x, size=x.shape).astype('float32')
            x = f_noise(x)
        return numpy.vstack(samples)

    def sampling_wrapper(NSI):
        # * is the "splat" operator: It takes a list as input, and expands it into actual positional arguments in the function call.
        out = f_sample2(*NSI)
        NSO = out[:len(network_state_output)]
        vis_pX_chain = out[len(network_state_output):]
        return NSO, vis_pX_chain

    def sample_some_numbers(N=400):
        # The network's initial state
        init_vis = test_X.get_value()[:1]

        noisy_init_vis = f_noise(init_vis)

        network_state = [
            [noisy_init_vis] + [numpy.zeros((1, len(b.get_value())), dtype='float32') for b in bias_list[1:]]]

        visible_chain = [init_vis]

        noisy_h0_chain = [noisy_init_vis]

        for i in range(N - 1):
            # feed the last state into the network, compute new state, and obtain visible units expectation chain
            net_state_out, vis_pX_chain = sampling_wrapper(network_state[-1])

            # append to the visible chain
            visible_chain += vis_pX_chain

            # append state output to the network state chain
            network_state.append(net_state_out)

            noisy_h0_chain.append(net_state_out[0])

        return numpy.vstack(visible_chain), numpy.vstack(noisy_h0_chain)

    def plot_samples(epoch_number, iteration):
        to_sample = time.time()
        if layers == 1:
            # one layer model
            V = sample_some_numbers_single_layer()
        else:
            V, H0 = sample_some_numbers()
        img_samples = PIL.Image.fromarray(tile_raster_images(V, (root_N_input, root_N_input), (20, 20)))

        fname = outdir + 'samples_iteration_' + str(iteration) + '_epoch_' + str(epoch_number) + '.png'
        img_samples.save(fname)
        print
        'Took ' + str(time.time() - to_sample) + ' to sample 400 numbers'

    ##############
    # Inpainting #
    ##############
    def inpainting(digit):
        # The network's initial state

        # NOISE INIT
        init_vis = cast32(numpy.random.uniform(size=digit.shape))

        # noisy_init_vis  =   f_noise(init_vis)
        # noisy_init_vis  =   cast32(numpy.random.uniform(size=init_vis.shape))

        # INDEXES FOR VISIBLE AND NOISY PART
        noise_idx = (numpy.arange(N_input) % root_N_input < (root_N_input / 2))
        fixed_idx = (numpy.arange(N_input) % root_N_input > (root_N_input / 2))

        # function to re-init the visible to the same noise

        # FUNCTION TO RESET HALF VISIBLE TO DIGIT
        def reset_vis(V):
            V[0][fixed_idx] = digit[0][fixed_idx]
            return V

        # INIT DIGIT : NOISE and RESET HALF TO DIGIT
        init_vis = reset_vis(init_vis)

        network_state = [[init_vis] + [numpy.zeros((1, len(b.get_value())), dtype='float32') for b in bias_list[1:]]]

        visible_chain = [init_vis]

        noisy_h0_chain = [init_vis]

        for i in range(49):
            # feed the last state into the network, compute new state, and obtain visible units expectation chain
            net_state_out, vis_pX_chain = sampling_wrapper(network_state[-1])

            # reset half the digit
            net_state_out[0] = reset_vis(net_state_out[0])
            vis_pX_chain[0] = reset_vis(vis_pX_chain[0])

            # append to the visible chain
            visible_chain += vis_pX_chain

            # append state output to the network state chain
            network_state.append(net_state_out)

            noisy_h0_chain.append(net_state_out[0])

        return numpy.vstack(visible_chain), numpy.vstack(noisy_h0_chain)

    def save_params_to_file(name, n, params, iteration):
        print
        'saving parameters...'
        save_path = outdir + name + '_params_iteration_' + str(iteration) + '_epoch_' + str(n) + '.pkl'
        f = open(save_path, 'wb')
        try:
            cPickle.dump(params, f, protocol=cPickle.HIGHEST_PROTOCOL)
        finally:
            f.close()

            ################

    # GSN TRAINING #
    ################
    def train_recurrent_GSN(iteration, train_X, train_Y, valid_X, valid_Y, test_X, test_Y):
        print
        '----------------------------------------'
        print
        'TRAINING GSN FOR ITERATION', iteration
        with open(logfile, 'a') as f:
            f.write("--------------------------\nTRAINING GSN FOR ITERATION {0!s}\n".format(iteration))

        # TRAINING
        n_epoch = state.n_epoch
        batch_size = state.batch_size
        STOP = False
        counter = 0
        if iteration == 0:
            learning_rate.set_value(cast32(state.learning_rate))  # learning rate
        times = []
        best_cost = float('inf')
        patience = 0

        print
        'learning rate:', learning_rate.get_value()

        print
        'train X size:', str(train_X.shape.eval())
        print
        'valid X size:', str(valid_X.shape.eval())
        print
        'test X size:', str(test_X.shape.eval())

        train_costs = []
        valid_costs = []
        test_costs = []
        train_costs_post = []
        valid_costs_post = []
        test_costs_post = []

        if state.vis_init:
            bias_list[0].set_value(logit(numpy.clip(0.9, 0.001, train_X.get_value().mean(axis=0))))

        if state.test_model:
            # If testing, do not train and go directly to generating samples, parzen window estimation, and inpainting
            print
            'Testing : skip training'
            STOP = True

        while not STOP:
            counter += 1
            t = time.time()
            print
            counter, '\t',
            with open(logfile, 'a') as f:
                f.write("{0!s}\t".format(counter))
            # shuffle the data
            data.sequence_mnist_data(train_X, train_Y, valid_X, valid_Y, test_X, test_Y, dataset, rng)

            # train
            # init hiddens
            #             hiddens = [(T.zeros_like(train_X[:batch_size]).eval())]
            #             for i in range(len(weights_list)):
            #                 # init with zeros
            #                 hiddens.append(T.zeros_like(T.dot(hiddens[i], weights_list[i])).eval())
            hiddens = [T.zeros((batch_size, layer_size)).eval() for layer_size in layer_sizes]
            train_cost = []
            train_cost_post = []
            for i in range(len(train_X.get_value(borrow=True)) / batch_size):
                xs = [train_X.get_value(borrow=True)[
                      (i * batch_size) + sequence_idx: ((i + 1) * batch_size) + sequence_idx] for sequence_idx in
                      range(len(Xs))]
                xs, hiddens = fix_input_size(xs, hiddens)
                hiddens[0] = xs[0]
                _ins = hiddens + xs
                _outs = f_learn(*_ins)
                hiddens = _outs[:len(hiddens)]
                cost = _outs[-2]
                cost_post = _outs[-1]
                train_cost.append(cost)
                train_cost_post.append(cost_post)

            train_cost = numpy.mean(train_cost)
            train_costs.append(train_cost)
            train_cost_post = numpy.mean(train_cost_post)
            train_costs_post.append(train_cost_post)
            print
            'Train : ', trunc(train_cost), trunc(train_cost_post), '\t',
            with open(logfile, 'a') as f:
                f.write("Train : {0!s} {1!s}\t".format(trunc(train_cost), trunc(train_cost_post)))
            with open(train_convergence_pre, 'a') as f:
                f.write("{0!s},".format(train_cost))
            with open(train_convergence_post, 'a') as f:
                f.write("{0!s},".format(train_cost_post))

            # valid
            # init hiddens
            hiddens = [T.zeros((batch_size, layer_size)).eval() for layer_size in layer_sizes]
            valid_cost = []
            valid_cost_post = []
            for i in range(len(valid_X.get_value(borrow=True)) / batch_size):
                xs = [valid_X.get_value(borrow=True)[
                      (i * batch_size) + sequence_idx: ((i + 1) * batch_size) + sequence_idx] for sequence_idx in
                      range(len(Xs))]
                xs, hiddens = fix_input_size(xs, hiddens)
                hiddens[0] = xs[0]
                _ins = hiddens + xs
                _outs = f_cost(*_ins)
                hiddens = _outs[:-2]
                cost = _outs[-2]
                cost_post = _outs[-1]
                valid_cost.append(cost)
                valid_cost_post.append(cost_post)

            valid_cost = numpy.mean(valid_cost)
            valid_costs.append(valid_cost)
            valid_cost_post = numpy.mean(valid_cost_post)
            valid_costs_post.append(valid_cost_post)
            print
            'Valid : ', trunc(valid_cost), trunc(valid_cost_post), '\t',
            with open(logfile, 'a') as f:
                f.write("Valid : {0!s} {1!s}\t".format(trunc(valid_cost), trunc(valid_cost_post)))
            with open(valid_convergence_pre, 'a') as f:
                f.write("{0!s},".format(valid_cost))
            with open(valid_convergence_post, 'a') as f:
                f.write("{0!s},".format(valid_cost_post))

            # test
            # init hiddens
            hiddens = [T.zeros((batch_size, layer_size)).eval() for layer_size in layer_sizes]
            test_cost = []
            test_cost_post = []
            for i in range(len(test_X.get_value(borrow=True)) / batch_size):
                xs = [test_X.get_value(borrow=True)[
                      (i * batch_size) + sequence_idx: ((i + 1) * batch_size) + sequence_idx] for sequence_idx in
                      range(len(Xs))]
                xs, hiddens = fix_input_size(xs, hiddens)
                hiddens[0] = xs[0]
                _ins = hiddens + xs
                _outs = f_cost(*_ins)
                hiddens = _outs[:-2]
                cost = _outs[-2]
                cost_post = _outs[-1]
                test_cost.append(cost)
                test_cost_post.append(cost_post)

            test_cost = numpy.mean(test_cost)
            test_costs.append(test_cost)
            test_cost_post = numpy.mean(test_cost_post)
            test_costs_post.append(test_cost_post)
            print
            'Test  : ', trunc(test_cost), trunc(test_cost_post), '\t',
            with open(logfile, 'a') as f:
                f.write("Test : {0!s} {1!s}\t".format(trunc(test_cost), trunc(test_cost_post)))
            with open(test_convergence_pre, 'a') as f:
                f.write("{0!s},".format(test_cost))
            with open(test_convergence_post, 'a') as f:
                f.write("{0!s},".format(test_cost_post))

            # check for early stopping
            cost = train_cost
            if cost < best_cost * state.early_stop_threshold:
                patience = 0
                best_cost = cost
            else:
                patience += 1

            if counter >= n_epoch or patience >= state.early_stop_length:
                STOP = True
                save_params_to_file('gsn', counter, params, iteration)

            timing = time.time() - t
            times.append(timing)

            print
            'time : ', trunc(timing),

            print
            'remaining: ', trunc((n_epoch - counter) * numpy.mean(times) / 60 / 60), 'hrs',

            print
            'B : ', [trunc(abs(b.get_value(borrow=True)).mean()) for b in bias_list],

            print
            'W : ', [trunc(abs(w.get_value(borrow=True)).mean()) for w in weights_list],

            print
            'V : ', [trunc(abs(v.get_value(borrow=True)).mean()) for v in recurrent_weights_list]

            with open(logfile, 'a') as f:
                f.write("MeanVisB : {0!s}\t".format(trunc(bias_list[0].get_value().mean())))

            with open(logfile, 'a') as f:
                f.write("W : {0!s}\t".format(str([trunc(abs(w.get_value(borrow=True)).mean()) for w in weights_list])))

            with open(logfile, 'a') as f:
                f.write("Time : {0!s} seconds\n".format(trunc(timing)))

            if (counter % state.save_frequency) == 0:
                # Checking reconstruction
                nums = test_X.get_value()[range(100)]
                noisy_nums = f_noise(test_X.get_value()[range(100)])
                reconstructed_prediction = []
                reconstructed_prediction_end = []
                # init reconstruction hiddens
                hiddens = [T.zeros(layer_size).eval() for layer_size in layer_sizes]
                for num in noisy_nums:
                    hiddens[0] = num
                    for i in range(len(hiddens)):
                        if len(hiddens[i].shape) == 2 and hiddens[i].shape[0] == 1:
                            hiddens[i] = hiddens[i][0]
                    _ins = hiddens + [num]
                    _outs = f_recon(*_ins)
                    hiddens = _outs[:len(hiddens)]
                    [reconstructed_1, reconstructed_n] = _outs[len(hiddens):]
                    reconstructed_prediction.append(reconstructed_1)
                    reconstructed_prediction_end.append(reconstructed_n)

                with open(logfile, 'a') as f:
                    f.write("\n")
                for i in range(len(nums)):
                    if len(reconstructed_prediction[i].shape) == 2 and reconstructed_prediction[i].shape[0] == 1:
                        reconstructed_prediction[i] = reconstructed_prediction[i][0]
                    print
                    nums[i].tolist(), "->", reconstructed_prediction[i].tolist()
                    with open(logfile, 'a') as f:
                        f.write("{0!s} -> {1!s}\n".format(nums[i].tolist(),
                                                          [trunc(n) if n > 0.0001 else trunc(0.00000000000000000) for n
                                                           in reconstructed_prediction[i].tolist()]))
                with open(logfile, 'a') as f:
                    f.write("\n")

                #                 # Concatenate stuff
                #                 stacked = numpy.vstack([numpy.vstack([nums[i*10 : (i+1)*10], noisy_nums[i*10 : (i+1)*10], reconstructed_prediction[i*10 : (i+1)*10], reconstructed_prediction_end[i*10 : (i+1)*10]]) for i in range(10)])
                #                 numbers_reconstruction = PIL.Image.fromarray(tile_raster_images(stacked, (root_N_input,root_N_input), (10,40)))
                #                 numbers_reconstruction.save(outdir+'gsn_number_reconstruction_iteration_'+str(iteration)+'_epoch_'+str(counter)+'.png')
                #
                #                 #sample_numbers(counter, 'seven')
                #                 plot_samples(counter, iteration)
                #
                #                 #save params
                #                 save_params_to_file('gsn', counter, params, iteration)

            # ANNEAL!
            new_lr = learning_rate.get_value() * annealing
            learning_rate.set_value(new_lr)

        # 10k samples
        print
        'Generating 10,000 samples'
        samples, _ = sample_some_numbers(N=10000)
        f_samples = outdir + 'samples.npy'
        numpy.save(f_samples, samples)
        print
        'saved digits'

    #####################
    # STORY 2 ALGORITHM #
    #####################
    for iter in range(state.max_iterations):
        train_recurrent_GSN(iter, train_X, train_Y, valid_X, valid_Y, test_X, test_Y)
