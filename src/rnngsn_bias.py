import os
import random as R
import time
from collections import OrderedDict

import PIL.Image
import cPickle
import numpy.random as rng
from recurrent_gsn import generative_stochastic_network
from utils import data_tools as data
from utils.image_tiler import tile_raster_images
from utils.logger import Logger
from utils.utils import *


def experiment(state, outdir_base='./'):
    rng.seed(1)  # seed the numpy random generator
    R.seed(1)  # seed the other random generator (for reconstruction function indices)
    # Initialize output directory and files
    data.mkdir_p(outdir_base)
    outdir = outdir_base + "/" + state.dataset + "/"
    data.mkdir_p(outdir)
    logger = Logger(outdir)
    logger.log("----------MODEL 2, {0!s}-----------\n".format(state.dataset))
    if state.initialize_gsn:
        gsn_train_convergence = outdir + "gsn_train_convergence.csv"
        gsn_valid_convergence = outdir + "gsn_valid_convergence.csv"
        gsn_test_convergence = outdir + "gsn_test_convergence.csv"
    train_convergence = outdir + "train_convergence.csv"
    valid_convergence = outdir + "valid_convergence.csv"
    test_convergence = outdir + "test_convergence.csv"
    if state.initialize_gsn:
        init_empty_file(gsn_train_convergence)
        init_empty_file(gsn_valid_convergence)
        init_empty_file(gsn_test_convergence)
    init_empty_file(train_convergence)
    init_empty_file(valid_convergence)
    init_empty_file(test_convergence)

    # load parameters from config file if this is a test
    config_filename = outdir + 'config'
    if state.test_model and 'config' in os.listdir(outdir):
        config_vals = load_from_config(config_filename)
        for CV in config_vals:
            logger.log(CV)
            if CV.startswith('test'):
                logger.log('Do not override testing switch')
                continue
            try:
                exec('state.' + CV) in globals(), locals()
            except:
                exec('state.' + CV.split('=')[0] + "='" + CV.split('=')[1] + "'") in globals(), locals()
    else:
        # Save the current configuration
        # Useful for logs/experiments
        logger.log('Saving config')
        with open(config_filename, 'w') as f:
            f.write(str(state))

    logger.log(state)

    ####################################################
    # Load the data, train = train+valid, and sequence #
    ####################################################
    artificial = False
    if state.dataset == 'MNIST_1' or state.dataset == 'MNIST_2' or state.dataset == 'MNIST_3':
        (train_X, train_Y), (valid_X, valid_Y), (test_X, test_Y) = data.load_mnist(state.data_path)
        train_X = numpy.concatenate((train_X, valid_X))
        train_Y = numpy.concatenate((train_Y, valid_Y))
        artificial = True
        try:
            dataset = int(state.dataset.split('_')[1])
        except:
            logger.log("ERROR: artificial dataset number not recognized. Input was " + str(state.dataset))
            raise AssertionError("artificial dataset number not recognized. Input was " + str(state.dataset))
    else:
        logger.log("ERROR: dataset not recognized.")
        raise AssertionError("dataset not recognized.")

    # transfer the datasets into theano shared variables
    train_X, train_Y = data.shared_dataset((train_X, train_Y), borrow=True)
    valid_X, valid_Y = data.shared_dataset((valid_X, valid_Y), borrow=True)
    test_X, test_Y = data.shared_dataset((test_X, test_Y), borrow=True)

    if artificial:
        logger.log('Sequencing MNIST data...')
        logger.log(['train set size:', len(train_Y.eval())])
        logger.log(['train set size:', len(valid_Y.eval())])
        logger.log(['train set size:', len(test_Y.eval())])
        data.sequence_mnist_data(train_X, train_Y, valid_X, valid_Y, test_X, test_Y, dataset, rng)
        logger.log(['train set size:', len(train_Y.eval())])
        logger.log(['train set size:', len(valid_Y.eval())])
        logger.log(['train set size:', len(test_Y.eval())])
        logger.log('Sequencing done.\n')

    N_input = train_X.eval().shape[1]
    root_N_input = numpy.sqrt(N_input)

    # Network and training specifications
    layers = state.layers  # number hidden layers
    walkbacks = state.walkbacks  # number of walkbacks
    layer_sizes = [N_input] + [state.hidden_size] * layers  # layer sizes, from h0 to hK (h0 is the visible layer)

    learning_rate = theano.shared(cast32(state.learning_rate))  # learning rate
    annealing = cast32(state.annealing)  # exponential annealing coefficient
    momentum = theano.shared(cast32(state.momentum))  # momentum term

    ##############
    # PARAMETERS #
    ##############
    # gsn
    weights_list = [get_shared_weights(layer_sizes[i], layer_sizes[i + 1], name="W_{0!s}_{1!s}".format(i, i + 1)) for i
                    in range(layers)]  # initialize each layer to uniform sample from sqrt(6. / (n_in + n_out))
    bias_list = [get_shared_bias(layer_sizes[i], name='b_' + str(i)) for i in
                 range(layers + 1)]  # initialize each layer to 0's.

    # recurrent
    recurrent_to_gsn_bias_weights_list = [
        get_shared_weights(state.recurrent_hidden_size, layer_sizes[layer], name="W_u_b{0!s}".format(layer)) for layer
        in range(layers + 1)]
    W_u_u = get_shared_weights(state.recurrent_hidden_size, state.recurrent_hidden_size, name="W_u_u")
    W_x_u = get_shared_weights(N_input, state.recurrent_hidden_size, name="W_x_u")
    recurrent_bias = get_shared_bias(state.recurrent_hidden_size, name='b_u')

    # lists for use with gradients
    gsn_params = weights_list + bias_list
    u_params = [W_u_u, W_x_u, recurrent_bias]
    params = gsn_params + recurrent_to_gsn_bias_weights_list + u_params

    ###########################################################
    #           load initial parameters of gsn                #
    ###########################################################
    train_gsn_first = False
    if state.initialize_gsn:
        params_to_load = 'gsn_params.pkl'
        if not os.path.isfile(params_to_load):
            train_gsn_first = True
        else:
            logger.log("\nLoading existing GSN parameters\n")
            loaded_params = cPickle.load(open(params_to_load, 'r'))
            [p.set_value(lp.get_value(borrow=False)) for lp, p in zip(loaded_params[:len(weights_list)], weights_list)]
            [p.set_value(lp.get_value(borrow=False)) for lp, p in zip(loaded_params[len(weights_list):], bias_list)]

    ############################
    # Theano variables and RNG #
    ############################
    MRG = RNG_MRG.MRG_RandomStreams(1)
    X = T.fmatrix('X')  # single (batch) for training gsn
    Xs = T.fmatrix(name="Xs")  # sequence for training rnn-gsn

    ########################
    # ACTIVATION FUNCTIONS #
    ########################
    # hidden activation
    if state.hidden_act == 'sigmoid':
        logger.log('Using sigmoid activation for hiddens')
        hidden_activation = T.nnet.sigmoid
    elif state.hidden_act == 'rectifier':
        logger.log('Using rectifier activation for hiddens')
        hidden_activation = lambda x: T.maximum(cast32(0), x)
    elif state.hidden_act == 'tanh':
        logger.log('Using hyperbolic tangent activation for hiddens')
        hidden_activation = lambda x: T.tanh(x)
    else:
        logger.log("ERROR: Did not recognize hidden activation {0!s}, please use tanh, rectifier, or sigmoid".format(
            state.hidden_act))
        raise NotImplementedError(
            "Did not recognize hidden activation {0!s}, please use tanh, rectifier, or sigmoid".format(
                state.hidden_act))

    # visible activation
    if state.visible_act == 'sigmoid':
        logger.log('Using sigmoid activation for visible layer')
        visible_activation = T.nnet.sigmoid
    elif state.visible_act == 'softmax':
        logger.log('Using softmax activation for visible layer')
        visible_activation = T.nnet.softmax
    else:
        logger.log("ERROR: Did not recognize visible activation {0!s}, please use sigmoid or softmax".format(
            state.visible_act))
        raise NotImplementedError(
            "Did not recognize visible activation {0!s}, please use sigmoid or softmax".format(state.visible_act))

    # recurrent activation
    if state.recurrent_hidden_act == 'sigmoid':
        logger.log('Using sigmoid activation for recurrent hiddens')
        recurrent_hidden_activation = T.nnet.sigmoid
    elif state.recurrent_hidden_act == 'rectifier':
        logger.log('Using rectifier activation for recurrent hiddens')
        recurrent_hidden_activation = lambda x: T.maximum(cast32(0), x)
    elif state.recurrent_hidden_act == 'tanh':
        logger.log('Using hyperbolic tangent activation for recurrent hiddens')
        recurrent_hidden_activation = lambda x: T.tanh(x)
    else:
        logger.log(
            "ERROR: Did not recognize recurrent hidden activation {0!s}, please use tanh, rectifier, or sigmoid".format(
                state.recurrent_hidden_act))
        raise NotImplementedError(
            "Did not recognize recurrent hidden activation {0!s}, please use tanh, rectifier, or sigmoid".format(
                state.recurrent_hidden_act))

    logger.log("\n")

    ####################
    #  COST FUNCTIONS  #
    ####################
    if state.cost_funct == 'binary_crossentropy':
        logger.log('Using binary cross-entropy cost!')
        cost_function = lambda x, y: T.mean(T.nnet.binary_crossentropy(x, y))
    elif state.cost_funct == 'square':
        logger.log("Using square error cost!")
        # cost_function = lambda x,y: T.log(T.mean(T.sqr(x-y)))
        cost_function = lambda x, y: T.log(T.sum(T.pow((x - y), 2)))
    else:
        logger.log("ERROR: Did not recognize cost function {0!s}, please use binary_crossentropy or square".format(
            state.cost_funct))
        raise NotImplementedError(
            "Did not recognize cost function {0!s}, please use binary_crossentropy or square".format(state.cost_funct))

    logger.log("\n")

    ##############################################
    #    Build the training graph for the GSN    #
    ##############################################       
    if train_gsn_first:
        '''Build the actual gsn training graph'''
        p_X_chain_gsn, _ = generative_stochastic_network.build_gsn(X,
                                                                   weights_list,
                                                                   bias_list,
                                                                   True,
                                                                   state.noiseless_h1,
                                                                   state.hidden_add_noise_sigma,
                                                                   state.input_salt_and_pepper,
                                                                   state.input_sampling,
                                                                   MRG,
                                                                   visible_activation,
                                                                   hidden_activation,
                                                                   walkbacks,
                                                                   logger)
        # now without noise
        p_X_chain_gsn_recon, _ = generative_stochastic_network.build_gsn(X,
                                                                         weights_list,
                                                                         bias_list,
                                                                         False,
                                                                         state.noiseless_h1,
                                                                         state.hidden_add_noise_sigma,
                                                                         state.input_salt_and_pepper,
                                                                         state.input_sampling,
                                                                         MRG,
                                                                         visible_activation,
                                                                         hidden_activation,
                                                                         walkbacks,
                                                                         logger)

    ##############################################
    #  Build the training graph for the RNN-GSN  #
    ##############################################
    # If `x_t` is given, deterministic recurrence to compute the u_t. Otherwise, first generate
    def recurrent_step(x_t, u_tm1):
        bv_t = bias_list[0] + T.dot(u_tm1, recurrent_to_gsn_bias_weights_list[0])
        bh_t = T.concatenate(
            [bias_list[i + 1] + T.dot(u_tm1, recurrent_to_gsn_bias_weights_list[i + 1]) for i in range(layers)], axis=0)
        generate = x_t is None
        if generate:
            pass
        ua_t = T.dot(x_t, W_x_u) + T.dot(u_tm1, W_u_u) + recurrent_bias
        u_t = recurrent_hidden_activation(ua_t)
        return None if generate else [ua_t, u_t, bv_t, bh_t]

    logger.log("\nCreating recurrent step scan.")
    # For training, the deterministic recurrence is used to compute all the
    # {h_t, 1 <= t <= T} given Xs. Conditional GSNs can then be trained
    # in batches using those parameters.
    u0 = T.zeros((state.recurrent_hidden_size,))  # initial value for the RNN hidden units
    (ua, u, bv_t, bh_t), updates_recurrent = theano.scan(fn=lambda x_t, u_tm1, *_: recurrent_step(x_t, u_tm1),
                                                         sequences=Xs,
                                                         outputs_info=[None, u0, None, None],
                                                         non_sequences=params)
    # put the bias_list together from hiddens and visible biases
    # b_list = [bv_t.flatten(2)] + [bh_t.dimshuffle((1,0,2))[i] for i in range(len(weights_list))]
    b_list = [bv_t] + [(bh_t.T[i * state.hidden_size:(i + 1) * state.hidden_size]).T for i in range(layers)]

    _, cost, show_cost = generative_stochastic_network.build_gsn_scan(Xs, weights_list, b_list, True,
                                                                      state.noiseless_h1, state.hidden_add_noise_sigma,
                                                                      state.input_salt_and_pepper, state.input_sampling,
                                                                      MRG, visible_activation, hidden_activation,
                                                                      walkbacks, cost_function, logger)
    x_sample_recon, _, _ = generative_stochastic_network.build_gsn_scan(Xs, weights_list, b_list, False,
                                                                        state.noiseless_h1,
                                                                        state.hidden_add_noise_sigma,
                                                                        state.input_salt_and_pepper,
                                                                        state.input_sampling, MRG, visible_activation,
                                                                        hidden_activation, walkbacks, cost_function,
                                                                        logger)

    updates_train = updates_recurrent
    # updates_train.update(updates_gsn)
    updates_cost = updates_recurrent

    # updates_recon = updates_recurrent
    # updates_recon.update(updates_gsn_recon)


    #############
    #   COSTS   #
    #############
    logger.log("")
    logger.log('Cost w.r.t p(X|...) at every step in the graph')

    if train_gsn_first:
        gsn_costs = [cost_function(rX, X) for rX in p_X_chain_gsn]
        gsn_show_cost = gsn_costs[-1]
        gsn_cost = numpy.sum(gsn_costs)

    ###################################
    # GRADIENTS AND FUNCTIONS FOR GSN #
    ###################################
    logger.log(["params:", params])

    logger.log("creating functions...")
    start_functions_time = time.time()

    if train_gsn_first:
        gradient_gsn = T.grad(gsn_cost, gsn_params)
        gradient_buffer_gsn = [theano.shared(numpy.zeros(param.get_value().shape, dtype='float32')) for param in
                               gsn_params]

        m_gradient_gsn = [momentum * gb + (cast32(1) - momentum) * g for (gb, g) in
                          zip(gradient_buffer_gsn, gradient_gsn)]
        param_updates_gsn = [(param, param - learning_rate * mg) for (param, mg) in zip(gsn_params, m_gradient_gsn)]
        gradient_buffer_updates_gsn = zip(gradient_buffer_gsn, m_gradient_gsn)

        grad_updates_gsn = OrderedDict(param_updates_gsn + gradient_buffer_updates_gsn)

        logger.log("gsn cost...")
        f_cost_gsn = theano.function(inputs=[X],
                                     outputs=gsn_show_cost,
                                     on_unused_input='warn')

        logger.log("gsn learn...")
        f_learn_gsn = theano.function(inputs=[X],
                                      updates=grad_updates_gsn,
                                      outputs=gsn_show_cost,
                                      on_unused_input='warn')

    #######################################
    # GRADIENTS AND FUNCTIONS FOR RNN-GSN #
    #######################################
    # if we are not using Hessian-free training create the normal sgd functions
    if state.hessian_free == 0:
        gradient = T.grad(cost, params)
        gradient_buffer = [theano.shared(numpy.zeros(param.get_value().shape, dtype='float32')) for param in params]

        m_gradient = [momentum * gb + (cast32(1) - momentum) * g for (gb, g) in zip(gradient_buffer, gradient)]
        param_updates = [(param, param - learning_rate * mg) for (param, mg) in zip(params, m_gradient)]
        gradient_buffer_updates = zip(gradient_buffer, m_gradient)

        updates = OrderedDict(param_updates + gradient_buffer_updates)
        updates_train.update(updates)

        logger.log("rnn-gsn learn...")
        f_learn = theano.function(inputs=[Xs],
                                  updates=updates_train,
                                  outputs=show_cost,
                                  on_unused_input='warn')

        logger.log("rnn-gsn cost...")
        f_cost = theano.function(inputs=[Xs],
                                 updates=updates_cost,
                                 outputs=show_cost,
                                 on_unused_input='warn')

    logger.log("Training/cost functions done.")
    compilation_time = time.time() - start_functions_time
    # Show the compile time with appropriate easy-to-read units.
    logger.log("Compilation took " + make_time_units_string(compilation_time) + ".\n\n")

    ############################################################################################
    # Denoise some numbers : show number, noisy number, predicted number, reconstructed number #
    ############################################################################################   
    # Recompile the graph without noise for reconstruction function
    # The layer update scheme
    logger.log("Creating graph for noisy reconstruction function at checkpoints during training.")
    f_recon = theano.function(inputs=[Xs], outputs=x_sample_recon[-1])

    # Now do the same but for the GSN in the initial run
    if train_gsn_first:
        f_recon_gsn = theano.function(inputs=[X], outputs=p_X_chain_gsn_recon[-1])

    logger.log("Done compiling all functions.")
    compilation_time = time.time() - start_functions_time
    # Show the compile time with appropriate easy-to-read units.
    logger.log("Total time took " + make_time_units_string(compilation_time) + ".\n\n")

    ############
    # Sampling #
    ############
    # a function to add salt and pepper noise
    f_noise = theano.function(inputs=[X], outputs=salt_and_pepper(X, state.input_salt_and_pepper))
    # the input to the sampling function
    X_sample = T.fmatrix("X_sampling")
    network_state_input = [X_sample] + [T.fmatrix("H_sampling_" + str(i + 1)) for i in range(layers)]

    # "Output" state of the network (noisy)
    # initialized with input, then we apply updates

    network_state_output = [X_sample] + network_state_input[1:]

    visible_pX_chain = []

    # ONE update
    logger.log("Performing one walkback in network state sampling.")
    generative_stochastic_network.update_layers(network_state_output,
                                                weights_list,
                                                bias_list,
                                                visible_pX_chain,
                                                True,
                                                state.noiseless_h1,
                                                state.hidden_add_noise_sigma,
                                                state.input_salt_and_pepper,
                                                state.input_sampling,
                                                MRG,
                                                visible_activation,
                                                hidden_activation,
                                                logger)

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

    def plot_samples(epoch_number, leading_text):
        to_sample = time.time()
        if layers == 1:
            # one layer model
            V = sample_some_numbers_single_layer()
        else:
            V, H0 = sample_some_numbers()
        img_samples = PIL.Image.fromarray(tile_raster_images(V, (root_N_input, root_N_input), (20, 20)))

        fname = outdir + leading_text + 'samples_epoch_' + str(epoch_number) + '.png'
        img_samples.save(fname)
        logger.log('Took ' + str(time.time() - to_sample) + ' to sample 400 numbers')

    #############################
    # Save the model parameters #
    #############################
    def save_params_to_file(name, n, gsn_params):
        pass

    #         print 'saving parameters...'
    #         save_path = outdir+name+'_params_epoch_'+str(n)+'.pkl'
    #         f = open(save_path, 'wb')
    #         try:
    #             cPickle.dump(gsn_params, f, protocol=cPickle.HIGHEST_PROTOCOL)
    #         finally:
    #             f.close()

    def save_params(params):
        values = [param.get_value(borrow=True) for param in params]
        return values

    def restore_params(params, values):
        for i in range(len(params)):
            params[i].set_value(values[i])

    ################
    # GSN TRAINING #
    ################
    def train_GSN(train_X, train_Y, valid_X, valid_Y, test_X, test_Y):
        logger.log("\n-----------TRAINING GSN------------\n")

        # TRAINING
        n_epoch = state.n_epoch
        batch_size = state.gsn_batch_size
        STOP = False
        counter = 0
        learning_rate.set_value(cast32(state.learning_rate))  # learning rate
        times = []
        best_cost = float('inf')
        best_params = None
        patience = 0

        logger.log(['train X size:', str(train_X.shape.eval())])
        logger.log(['valid X size:', str(valid_X.shape.eval())])
        logger.log(['test X size:', str(test_X.shape.eval())])

        if state.vis_init:
            bias_list[0].set_value(logit(numpy.clip(0.9, 0.001, train_X.get_value().mean(axis=0))))

        if state.test_model:
            # If testing, do not train and go directly to generating samples, parzen window estimation, and inpainting
            logger.log('Testing : skip training')
            STOP = True

        while not STOP:
            counter += 1
            t = time.time()
            logger.append([counter, '\t'])

            # shuffle the data
            data.sequence_mnist_data(train_X, train_Y, valid_X, valid_Y, test_X, test_Y, dataset, rng)

            # train
            train_costs = []
            for i in xrange(len(train_X.get_value(borrow=True)) / batch_size):
                x = train_X.get_value()[i * batch_size: (i + 1) * batch_size]
                cost = f_learn_gsn(x)
                train_costs.append([cost])
            train_costs = numpy.mean(train_costs)
            # record it
            logger.append(['Train:', trunc(train_costs), '\t'])
            with open(gsn_train_convergence, 'a') as f:
                f.write("{0!s},".format(train_costs))
                f.write("\n")

            # valid
            valid_costs = []
            for i in xrange(len(valid_X.get_value(borrow=True)) / batch_size):
                x = valid_X.get_value()[i * batch_size: (i + 1) * batch_size]
                cost = f_cost_gsn(x)
                valid_costs.append([cost])
            valid_costs = numpy.mean(valid_costs)
            # record it
            logger.append(['Valid:', trunc(valid_costs), '\t'])
            with open(gsn_valid_convergence, 'a') as f:
                f.write("{0!s},".format(valid_costs))
                f.write("\n")

            # test
            test_costs = []
            for i in xrange(len(test_X.get_value(borrow=True)) / batch_size):
                x = test_X.get_value()[i * batch_size: (i + 1) * batch_size]
                cost = f_cost_gsn(x)
                test_costs.append([cost])
            test_costs = numpy.mean(test_costs)
            # record it 
            logger.append(['Test:', trunc(test_costs), '\t'])
            with open(gsn_test_convergence, 'a') as f:
                f.write("{0!s},".format(test_costs))
                f.write("\n")

            # check for early stopping
            cost = numpy.sum(valid_costs)
            if cost < best_cost * state.early_stop_threshold:
                patience = 0
                best_cost = cost
                # save the parameters that made it the best
                best_params = save_params(gsn_params)
            else:
                patience += 1

            if counter >= n_epoch or patience >= state.early_stop_length:
                STOP = True
                if best_params is not None:
                    restore_params(gsn_params, best_params)
                save_params_to_file('gsn', counter, gsn_params)

            timing = time.time() - t
            times.append(timing)

            logger.append('time: ' + make_time_units_string(timing) + '\t')

            logger.log('remaining: ' + make_time_units_string((n_epoch - counter) * numpy.mean(times)))

            if (counter % state.save_frequency) == 0 or STOP is True:
                n_examples = 100
                random_idx = numpy.array(R.sample(range(len(test_X.get_value(borrow=True))), n_examples))
                numbers = test_X.get_value(borrow=True)[random_idx]
                noisy_numbers = f_noise(test_X.get_value(borrow=True)[random_idx])
                reconstructed = f_recon_gsn(noisy_numbers)
                # Concatenate stuff
                stacked = numpy.vstack([numpy.vstack(
                    [numbers[i * 10: (i + 1) * 10], noisy_numbers[i * 10: (i + 1) * 10],
                     reconstructed[i * 10: (i + 1) * 10]]) for i in range(10)])
                number_reconstruction = PIL.Image.fromarray(
                    tile_raster_images(stacked, (root_N_input, root_N_input), (10, 30)))

                number_reconstruction.save(outdir + 'gsn_number_reconstruction_epoch_' + str(counter) + '.png')

                # sample_numbers(counter, 'seven')
                plot_samples(counter, 'gsn')

                # save gsn_params
                save_params_to_file('gsn', counter, gsn_params)

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

    def train_RNN_GSN(train_X, train_Y, valid_X, valid_Y, test_X, test_Y):
        # If we are using Hessian-free training
        if state.hessian_free == 1:
            pass
        #         gradient_dataset = hf_sequence_dataset([train_X.get_value()], batch_size=None, number_batches=5000)
        #         cg_dataset = hf_sequence_dataset([train_X.get_value()], batch_size=None, number_batches=1000)
        #         valid_dataset = hf_sequence_dataset([valid_X.get_value()], batch_size=None, number_batches=1000)
        #
        #         s = x_samples
        #         costs = [cost, show_cost]
        #         hf_optimizer(params, [Xs], s, costs, u, ua).train(gradient_dataset, cg_dataset, initial_lambda=1.0, preconditioner=True, validation=valid_dataset)

        # If we are using SGD training
        else:
            # Define the re-used loops for f_learn and f_cost
            def apply_cost_function_to_dataset(function, dataset):
                costs = []
                for i in xrange(len(dataset.get_value(borrow=True)) / batch_size):
                    xs = dataset.get_value(borrow=True)[i * batch_size: (i + 1) * batch_size]
                    cost = function(xs)
                    costs.append([cost])
                return numpy.mean(costs)

            logger.log("\n-----------TRAINING RNN-GSN------------\n")
            # TRAINING
            n_epoch = state.n_epoch
            batch_size = state.batch_size
            STOP = False
            counter = 0
            learning_rate.set_value(cast32(state.learning_rate))  # learning rate
            times = []
            best_cost = float('inf')
            best_params = None
            patience = 0

            logger.log(['train X size:', str(train_X.shape.eval())])
            logger.log(['valid X size:', str(valid_X.shape.eval())])
            logger.log(['test X size:', str(test_X.shape.eval())])

            if state.vis_init:
                bias_list[0].set_value(logit(numpy.clip(0.9, 0.001, train_X.get_value().mean(axis=0))))

            if state.test_model:
                # If testing, do not train and go directly to generating samples, parzen window estimation, and inpainting
                logger.log('Testing : skip training')
                STOP = True

            while not STOP:
                counter += 1
                t = time.time()
                logger.append([counter, '\t'])

                # shuffle the data
                data.sequence_mnist_data(train_X, train_Y, valid_X, valid_Y, test_X, test_Y, dataset, rng)

                # train
                train_costs = apply_cost_function_to_dataset(f_learn, train_X)
                # record it
                logger.append(['Train:', trunc(train_costs), '\t'])
                with open(train_convergence, 'a') as f:
                    f.write("{0!s},".format(train_costs))
                    f.write("\n")

                # valid
                valid_costs = apply_cost_function_to_dataset(f_cost, valid_X)
                # record it
                logger.append(['Valid:', trunc(valid_costs), '\t'])
                with open(valid_convergence, 'a') as f:
                    f.write("{0!s},".format(valid_costs))
                    f.write("\n")

                # test
                test_costs = apply_cost_function_to_dataset(f_cost, test_X)
                # record it 
                logger.append(['Test:', trunc(test_costs), '\t'])
                with open(test_convergence, 'a') as f:
                    f.write("{0!s},".format(test_costs))
                    f.write("\n")

                # check for early stopping
                cost = numpy.sum(valid_costs)
                if cost < best_cost * state.early_stop_threshold:
                    patience = 0
                    best_cost = cost
                    # save the parameters that made it the best
                    best_params = save_params(params)
                else:
                    patience += 1

                if counter >= n_epoch or patience >= state.early_stop_length:
                    STOP = True
                    if best_params is not None:
                        restore_params(params, best_params)
                    save_params_to_file('all', counter, params)

                timing = time.time() - t
                times.append(timing)

                logger.append('time: ' + make_time_units_string(timing) + '\t')

                logger.log('remaining: ' + make_time_units_string((n_epoch - counter) * numpy.mean(times)))

                if (counter % state.save_frequency) == 0 or STOP is True:
                    n_examples = 100
                    nums = test_X.get_value(borrow=True)[range(n_examples)]
                    noisy_nums = f_noise(test_X.get_value(borrow=True)[range(n_examples)])
                    reconstructions = []
                    for i in xrange(0, len(noisy_nums)):
                        recon = f_recon(noisy_nums[max(0, (i + 1) - batch_size):i + 1])
                        reconstructions.append(recon)
                    reconstructed = numpy.array(reconstructions)

                    # Concatenate stuff
                    stacked = numpy.vstack([numpy.vstack([nums[i * 10: (i + 1) * 10], noisy_nums[i * 10: (i + 1) * 10],
                                                          reconstructed[i * 10: (i + 1) * 10]]) for i in range(10)])
                    number_reconstruction = PIL.Image.fromarray(
                        tile_raster_images(stacked, (root_N_input, root_N_input), (10, 30)))

                    number_reconstruction.save(outdir + 'rnngsn_number_reconstruction_epoch_' + str(counter) + '.png')

                    # sample_numbers(counter, 'seven')
                    plot_samples(counter, 'rnngsn')

                    # save params
                    save_params_to_file('all', counter, params)

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
    # train the GSN parameters first to get a good baseline (if not loaded from parameter .pkl file)
    if train_gsn_first:
        train_GSN(train_X, train_Y, valid_X, valid_Y, test_X, test_Y)
    # train the entire RNN-GSN
    train_RNN_GSN(train_X, train_Y, valid_X, valid_Y, test_X, test_Y)
