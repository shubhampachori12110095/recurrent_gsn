'''
@author: Markus Beissinger
University of Pennsylvania, 2014-2015

This class produces the model discussed in the paper: (my sen paper)

'''

import time
from collections import OrderedDict

import PIL.Image
import cPickle
import numpy
import numpy.random as rng
import theano
import theano.sandbox.rng_mrg as RNG_MRG
import theano.tensor as T
import utils.logger as log
from recurrent_gsn import generative_stochastic_network
from utils import data_tools as data
from utils.image_tiler import tile_raster_images
from utils.utils import cast32, logit, trunc, get_shared_weights, get_shared_bias, salt_and_pepper, \
    make_time_units_string

# Default values to use for SEN parameters
defaults = {  # gsn parameters
    "gsn_layers": 3,  # number of hidden layers to use
    "walkbacks": 5,
# number of walkbacks (generally 2*layers) - need enough to have info from top layer propagate to visible layer
    "hidden_size": 1500,
    "hidden_activation": lambda x: T.tanh(x),
    "visible_activation": lambda x: T.nnet.sigmoid(x),
    "input_sampling": True,
    "MRG": RNG_MRG.MRG_RandomStreams(1),
    # recurrent parameters
    "recurrent_hidden_size": 1500,
    "recurrent_hidden_activation": lambda x: T.tanh(x),
    # sen parameters

    # training parameters
    "load_params": False,
    "cost_function": lambda x, y: T.mean(T.nnet.binary_crossentropy(x, y)),
    "n_epoch": 1000,
    "gsn_batch_size": 100,
    "batch_size": 200,
    "save_frequency": 10,
    "early_stop_threshold": .9995,
    "early_stop_length": 30,
    "hessian_free": False,
    "learning_rate": 0.25,
    "annealing": 0.995,
    "momentum": 0.5,
    "regularize_weight": 0,
    # noise parameters
    "add_noise": True,
    "noiseless_h1": True,
    "hidden_add_noise_sigma": 2,
    "input_salt_and_pepper": 0.4,
    "noise_annealing": 1.0,  # no noise schedule by default
    # data parameters
    "is_image": True,
    "vis_init": False,
    "output_path": '../outputs/sen/'}


class SEN():
    '''
    Class for creating a new Sequence Encoder Network (SEN)
    '''

    def __init__(self, train_X=None, train_Y=None, valid_X=None, valid_Y=None, test_X=None, test_Y=None, args=None,
                 logger=None):
        # Output logger
        self.logger = logger
        self.outdir = args.get("output_path", defaults["output_path"])
        if self.outdir[-1] != '/':
            self.outdir = self.outdir + '/'
        # Input data
        self.train_X = train_X
        self.train_Y = train_Y
        self.valid_X = valid_X
        self.valid_Y = valid_Y
        self.test_X = test_X
        self.test_Y = test_Y

        # variables from the dataset that are used for initialization and image reconstruction
        if train_X is None:
            self.N_input = args.get("input_size")
            if args.get("input_size") is None:
                raise AssertionError(
                    "Please either specify input_size in the arguments or provide an example train_X for input dimensionality.")
        else:
            self.N_input = train_X.eval().shape[1]
        self.root_N_input = numpy.sqrt(self.N_input)

        self.is_image = args.get('is_image', defaults['is_image'])
        if self.is_image:
            self.image_width = args.get('width', self.root_N_input)
            self.image_height = args.get('height', self.root_N_input)

        #######################################
        # Network and training specifications #
        #######################################
        self.gsn_layers = args.get('gsn_layers', defaults['gsn_layers'])  # number hidden layers
        self.walkbacks = args.get('walkbacks', defaults['walkbacks'])  # number of walkbacks
        self.learning_rate = theano.shared(
            cast32(args.get('learning_rate', defaults['learning_rate'])))  # learning rate
        self.init_learn_rate = cast32(args.get('learning_rate', defaults['learning_rate']))
        self.momentum = theano.shared(cast32(args.get('momentum', defaults['momentum'])))  # momentum term
        self.annealing = cast32(args.get('annealing', defaults['annealing']))  # exponential annealing coefficient
        self.noise_annealing = cast32(
            args.get('noise_annealing', defaults['noise_annealing']))  # exponential noise annealing coefficient
        self.batch_size = args.get('batch_size', defaults['batch_size'])
        self.gsn_batch_size = args.get('gsn_batch_size', defaults['gsn_batch_size'])
        self.n_epoch = args.get('n_epoch', defaults['n_epoch'])
        self.early_stop_threshold = args.get('early_stop_threshold', defaults['early_stop_threshold'])
        self.early_stop_length = args.get('early_stop_length', defaults['early_stop_length'])
        self.save_frequency = args.get('save_frequency', defaults['save_frequency'])

        self.noiseless_h1 = args.get('noiseless_h1', defaults["noiseless_h1"])
        self.hidden_add_noise_sigma = theano.shared(
            cast32(args.get('hidden_add_noise_sigma', defaults["hidden_add_noise_sigma"])))
        self.input_salt_and_pepper = theano.shared(
            cast32(args.get('input_salt_and_pepper', defaults["input_salt_and_pepper"])))
        self.input_sampling = args.get('input_sampling', defaults["input_sampling"])
        self.vis_init = args.get('vis_init', defaults['vis_init'])
        self.load_params = args.get('load_params', defaults['load_params'])
        self.hessian_free = args.get('hessian_free', defaults['hessian_free'])

        self.layer_sizes = [self.N_input] + [args.get('hidden_size', defaults[
            'hidden_size'])] * self.gsn_layers  # layer sizes, from h0 to hK (h0 is the visible layer)
        self.recurrent_hidden_size = args.get('recurrent_hidden_size', defaults['recurrent_hidden_size'])
        self.top_layer_sizes = [self.recurrent_hidden_size] + [args.get('hidden_size', defaults[
            'hidden_size'])] * self.gsn_layers  # layer sizes, from h0 to hK (h0 is the visible layer)

        self.f_recon = None
        self.f_noise = None

        # Activation functions!
        # For the GSN:
        if args.get('hidden_activation') is not None:
            log.maybeLog(self.logger, 'Using specified activation for GSN hiddens')
            self.hidden_activation = args.get('hidden_activation')
        elif args.get('hidden_act') == 'sigmoid':
            log.maybeLog(self.logger, 'Using sigmoid activation for GSN hiddens')
            self.hidden_activation = T.nnet.sigmoid
        elif args.get('hidden_act') == 'rectifier':
            log.maybeLog(self.logger, 'Using rectifier activation for GSN hiddens')
            self.hidden_activation = lambda x: T.maximum(cast32(0), x)
        elif args.get('hidden_act') == 'tanh':
            log.maybeLog(self.logger, 'Using hyperbolic tangent activation for GSN hiddens')
            self.hidden_activation = lambda x: T.tanh(x)
        elif args.get('hidden_act') is not None:
            log.maybeLog(self.logger,
                         "Did not recognize hidden activation {0!s}, please use tanh, rectifier, or sigmoid for GSN hiddens".format(
                             args.get('hidden_act')))
            raise NotImplementedError(
                "Did not recognize hidden activation {0!s}, please use tanh, rectifier, or sigmoid for GSN hiddens".format(
                    args.get('hidden_act')))
        else:
            log.maybeLog(self.logger, "Using default activation for GSN hiddens")
            self.hidden_activation = defaults['hidden_activation']
        # For the RNN:
        if args.get('recurrent_hidden_activation') is not None:
            log.maybeLog(self.logger, 'Using specified activation for RNN hiddens')
            self.recurrent_hidden_activation = args.get('recurrent_hidden_activation')
        elif args.get('recurrent_hidden_act') == 'sigmoid':
            log.maybeLog(self.logger, 'Using sigmoid activation for RNN hiddens')
            self.recurrent_hidden_activation = T.nnet.sigmoid
        elif args.get('recurrent_hidden_act') == 'rectifier':
            log.maybeLog(self.logger, 'Using rectifier activation for RNN hiddens')
            self.recurrent_hidden_activation = lambda x: T.maximum(cast32(0), x)
        elif args.get('recurrent_hidden_act') == 'tanh':
            log.maybeLog(self.logger, 'Using hyperbolic tangent activation for RNN hiddens')
            self.recurrent_hidden_activation = lambda x: T.tanh(x)
        elif args.get('recurrent_hidden_act') is not None:
            log.maybeLog(self.logger,
                         "Did not recognize hidden activation {0!s}, please use tanh, rectifier, or sigmoid for RNN hiddens".format(
                             args.get('hidden_act')))
            raise NotImplementedError(
                "Did not recognize hidden activation {0!s}, please use tanh, rectifier, or sigmoid for RNN hiddens".format(
                    args.get('hidden_act')))
        else:
            log.maybeLog(self.logger, "Using default activation for RNN hiddens")
            self.recurrent_hidden_activation = defaults['recurrent_hidden_activation']
        # Visible layer activation
        if args.get('visible_activation') is not None:
            log.maybeLog(self.logger, 'Using specified activation for visible layer')
            self.visible_activation = args.get('visible_activation')
        elif args.get('visible_act') == 'sigmoid':
            log.maybeLog(self.logger, 'Using sigmoid activation for visible layer')
            self.visible_activation = T.nnet.sigmoid
        elif args.get('visible_act') == 'softmax':
            log.maybeLog(self.logger, 'Using softmax activation for visible layer')
            self.visible_activation = T.nnet.softmax
        elif args.get('visible_act') is not None:
            log.maybeLog(self.logger,
                         "Did not recognize visible activation {0!s}, please use sigmoid or softmax".format(
                             args.get('visible_act')))
            raise NotImplementedError(
                "Did not recognize visible activation {0!s}, please use sigmoid or softmax".format(
                    args.get('visible_act')))
        else:
            log.maybeLog(self.logger, 'Using default activation for visible layer')
            self.visible_activation = defaults['visible_activation']

        # Cost function!
        if args.get('cost_function') is not None:
            log.maybeLog(self.logger, '\nUsing specified cost function for GSN training\n')
            self.cost_function = args.get('cost_function')
        elif args.get('cost_funct') == 'binary_crossentropy':
            log.maybeLog(self.logger, '\nUsing binary cross-entropy cost!\n')
            self.cost_function = lambda x, y: T.mean(T.nnet.binary_crossentropy(x, y))
        elif args.get('cost_funct') == 'square':
            log.maybeLog(self.logger, "\nUsing square error cost!\n")
            # cost_function = lambda x,y: T.log(T.mean(T.sqr(x-y)))
            self.cost_function = lambda x, y: T.log(T.sum(T.pow((x - y), 2)))
        elif args.get('cost_funct') is not None:
            log.maybeLog(self.logger,
                         "\nDid not recognize cost function {0!s}, please use binary_crossentropy or square\n".format(
                             args.get('cost_funct')))
            raise NotImplementedError(
                "Did not recognize cost function {0!s}, please use binary_crossentropy or square".format(
                    args.get('cost_funct')))
        else:
            log.maybeLog(self.logger, '\nUsing default cost function for GSN training\n')
            self.cost_function = defaults['cost_function']

        ############################
        # Theano variables and RNG #
        ############################
        self.X = T.fmatrix('X')  # single (batch) for training gsn
        self.Xs = T.fmatrix('Xs')  # sequence for training rnn
        self.MRG = RNG_MRG.MRG_RandomStreams(1)

        ###############
        # Parameters! #
        ###############
        # visible gsn
        self.weights_list = [
            get_shared_weights(self.layer_sizes[i], self.layer_sizes[i + 1], name="W_{0!s}_{1!s}".format(i, i + 1)) for
            i in range(self.gsn_layers)]  # initialize each layer to uniform sample from sqrt(6. / (n_in + n_out))
        self.bias_list = [get_shared_bias(self.layer_sizes[i], name='b_' + str(i)) for i in
                          range(self.gsn_layers + 1)]  # initialize each layer to 0's.

        # recurrent
        self.recurrent_to_gsn_weights_list = [
            get_shared_weights(self.recurrent_hidden_size, self.layer_sizes[layer], name="W_u_h{0!s}".format(layer)) for
            layer in range(self.gsn_layers + 1) if layer % 2 != 0]
        self.W_u_u = get_shared_weights(self.recurrent_hidden_size, self.recurrent_hidden_size, name="W_u_u")
        self.W_ins_u = get_shared_weights(args.get('hidden_size', defaults['hidden_size']), self.recurrent_hidden_size,
                                          name="W_ins_u")
        self.recurrent_bias = get_shared_bias(self.recurrent_hidden_size, name='b_u')

        # top layer gsn
        self.top_weights_list = [get_shared_weights(self.top_layer_sizes[i], self.top_layer_sizes[i + 1],
                                                    name="Wtop_{0!s}_{1!s}".format(i, i + 1)) for i in range(
            self.gsn_layers)]  # initialize each layer to uniform sample from sqrt(6. / (n_in + n_out))
        self.top_bias_list = [get_shared_bias(self.top_layer_sizes[i], name='btop_' + str(i)) for i in
                              range(self.gsn_layers + 1)]  # initialize each layer to 0's.

        # lists for use with gradients
        self.gsn_params = self.weights_list + self.bias_list
        self.u_params = [self.W_u_u, self.W_ins_u, self.recurrent_bias]
        self.top_params = self.top_weights_list + self.top_bias_list
        self.params = self.gsn_params + self.recurrent_to_gsn_weights_list + self.u_params + self.top_params

        ###################################################
        #          load initial parameters                #
        ###################################################
        if self.load_params:
            params_to_load = 'gsn_params.pkl'
            log.maybeLog(self.logger, "\nLoading existing GSN parameters\n")
            loaded_params = cPickle.load(open(params_to_load, 'r'))
            [p.set_value(lp.get_value(borrow=False)) for lp, p in
             zip(loaded_params[:len(self.weights_list)], self.weights_list)]
            [p.set_value(lp.get_value(borrow=False)) for lp, p in
             zip(loaded_params[len(self.weights_list):], self.bias_list)]

            params_to_load = 'rnn_params.pkl'
            log.maybeLog(self.logger, "\nLoading existing RNN parameters\n")
            loaded_params = cPickle.load(open(params_to_load, 'r'))
            [p.set_value(lp.get_value(borrow=False)) for lp, p in
             zip(loaded_params[:len(self.recurrent_to_gsn_weights_list)], self.recurrent_to_gsn_weights_list)]
            [p.set_value(lp.get_value(borrow=False)) for lp, p in
             zip(loaded_params[len(self.recurrent_to_gsn_weights_list):len(self.recurrent_to_gsn_weights_list) + 1],
                 self.W_u_u)]
            [p.set_value(lp.get_value(borrow=False)) for lp, p in
             zip(loaded_params[len(self.recurrent_to_gsn_weights_list) + 1:len(self.recurrent_to_gsn_weights_list) + 2],
                 self.W_ins_u)]
            [p.set_value(lp.get_value(borrow=False)) for lp, p in
             zip(loaded_params[len(self.recurrent_to_gsn_weights_list) + 2:], self.recurrent_bias)]

            params_to_load = 'top_gsn_params.pkl'
            log.maybeLog(self.logger, "\nLoading existing top level GSN parameters\n")
            loaded_params = cPickle.load(open(params_to_load, 'r'))
            [p.set_value(lp.get_value(borrow=False)) for lp, p in
             zip(loaded_params[:len(self.top_weights_list)], self.top_weights_list)]
            [p.set_value(lp.get_value(borrow=False)) for lp, p in
             zip(loaded_params[len(self.top_weights_list):], self.top_bias_list)]

        self.gsn_args = {'weights_list': self.weights_list,
                         'bias_list': self.bias_list,
                         'hidden_activation': self.hidden_activation,
                         'visible_activation': self.visible_activation,
                         'cost_function': self.cost_function,
                         'layers': self.gsn_layers,
                         'walkbacks': self.walkbacks,
                         'hidden_size': args.get('hidden_size', defaults['hidden_size']),
                         'learning_rate': args.get('learning_rate', defaults['learning_rate']),
                         'momentum': args.get('momentum', defaults['momentum']),
                         'annealing': self.annealing,
                         'noise_annealing': self.noise_annealing,
                         'batch_size': self.gsn_batch_size,
                         'n_epoch': self.n_epoch,
                         'early_stop_threshold': self.early_stop_threshold,
                         'early_stop_length': self.early_stop_length,
                         'save_frequency': self.save_frequency,
                         'noiseless_h1': self.noiseless_h1,
                         'hidden_add_noise_sigma': args.get('hidden_add_noise_sigma',
                                                            defaults['hidden_add_noise_sigma']),
                         'input_salt_and_pepper': args.get('input_salt_and_pepper', defaults['input_salt_and_pepper']),
                         'input_sampling': self.input_sampling,
                         'vis_init': self.vis_init,
                         'output_path': self.outdir + 'gsn/',
                         'is_image': self.is_image,
                         'input_size': self.N_input
                         }

        self.top_gsn_args = {'weights_list': self.top_weights_list,
                             'bias_list': self.top_bias_list,
                             'hidden_activation': self.hidden_activation,
                             'visible_activation': self.recurrent_hidden_activation,
                             'cost_function': self.cost_function,
                             'layers': self.gsn_layers,
                             'walkbacks': self.walkbacks,
                             'hidden_size': args.get('hidden_size', defaults['hidden_size']),
                             'learning_rate': args.get('learning_rate', defaults['learning_rate']),
                             'momentum': args.get('momentum', defaults['momentum']),
                             'annealing': self.annealing,
                             'noise_annealing': self.noise_annealing,
                             'batch_size': self.gsn_batch_size,
                             'n_epoch': self.n_epoch,
                             'early_stop_threshold': self.early_stop_threshold,
                             'early_stop_length': self.early_stop_length,
                             'save_frequency': self.save_frequency,
                             'noiseless_h1': self.noiseless_h1,
                             'hidden_add_noise_sigma': args.get('hidden_add_noise_sigma',
                                                                defaults['hidden_add_noise_sigma']),
                             'input_salt_and_pepper': args.get('input_salt_and_pepper',
                                                               defaults['input_salt_and_pepper']),
                             'input_sampling': self.input_sampling,
                             'vis_init': self.vis_init,
                             'output_path': self.outdir + 'top_gsn/',
                             'is_image': False,
                             'input_size': self.recurrent_hidden_size
                             }

        ############
        # Sampling #
        ############
        # the input to the sampling function
        X_sample = T.fmatrix("X_sampling")
        self.network_state_input = [X_sample] + [T.fmatrix("H_sampling_" + str(i + 1)) for i in range(self.gsn_layers)]

        # "Output" state of the network (noisy)
        # initialized with input, then we apply updates
        self.network_state_output = [X_sample] + self.network_state_input[1:]
        visible_pX_chain = []

        # ONE update
        log.maybeLog(self.logger, "Performing one walkback in network state sampling.")
        generative_stochastic_network.update_layers(self.network_state_output,
                                                    self.weights_list,
                                                    self.bias_list,
                                                    visible_pX_chain,
                                                    True,
                                                    self.noiseless_h1,
                                                    self.hidden_add_noise_sigma,
                                                    self.input_salt_and_pepper,
                                                    self.input_sampling,
                                                    self.MRG,
                                                    self.visible_activation,
                                                    self.hidden_activation,
                                                    self.logger)

        ##############################################
        #        Build the graphs for the SEN        #
        ##############################################
        # If `x_t` is given, deterministic recurrence to compute the u_t. Otherwise, first generate
        def recurrent_step(x_t, u_tm1, add_noise):
            # Make current guess for hiddens based on U
            for i in range(self.gsn_layers):
                if i % 2 == 0:
                    log.maybeLog(self.logger,
                                 "Using {0!s} and {1!s}".format(self.recurrent_to_gsn_weights_list[(i + 1) / 2],
                                                                self.bias_list[i + 1]))
            h_t = T.concatenate([self.hidden_activation(
                self.bias_list[i + 1] + T.dot(u_tm1, self.recurrent_to_gsn_weights_list[(i + 1) / 2])) for i in
                                 range(self.gsn_layers) if i % 2 == 0], axis=0)

            # Make a GSN to update U
            _, hs = generative_stochastic_network.build_gsn(x_t, self.weights_list, self.bias_list, add_noise,
                                                            self.noiseless_h1, self.hidden_add_noise_sigma,
                                                            self.input_salt_and_pepper, self.input_sampling, self.MRG,
                                                            self.visible_activation, self.hidden_activation,
                                                            self.walkbacks, self.logger)
            htop_t = hs[-1]
            ins_t = htop_t

            ua_t = T.dot(ins_t, self.W_ins_u) + T.dot(u_tm1, self.W_u_u) + self.recurrent_bias
            u_t = self.recurrent_hidden_activation(ua_t)
            return [ua_t, u_t, h_t]

        log.maybeLog(self.logger, "\nCreating recurrent step scan.")
        # For training, the deterministic recurrence is used to compute all the
        # {h_t, 1 <= t <= T} given Xs. Conditional GSNs can then be trained
        # in batches using those parameters.
        u0 = T.zeros((self.recurrent_hidden_size,))  # initial value for the RNN hidden units
        (ua, u, h_t), updates_recurrent = theano.scan(fn=lambda x_t, u_tm1, *_: recurrent_step(x_t, u_tm1, True),
                                                      sequences=self.Xs,
                                                      outputs_info=[None, u0, None],
                                                      non_sequences=self.params)

        log.maybeLog(self.logger, "Now for reconstruction sample without noise")
        (_, _, h_t_recon), updates_recurrent_recon = theano.scan(
            fn=lambda x_t, u_tm1, *_: recurrent_step(x_t, u_tm1, False),
            sequences=self.Xs,
            outputs_info=[None, u0, None],
            non_sequences=self.params)
        # put together the hiddens list
        h_list = [T.zeros_like(self.Xs)]
        for layer, w in enumerate(self.weights_list):
            if layer % 2 != 0:
                h_list.append(T.zeros_like(T.dot(h_list[-1], w)))
            else:
                h_list.append((h_t.T[(layer / 2) * self.hidden_size:(layer / 2 + 1) * self.hidden_size]).T)

        h_list_recon = [T.zeros_like(self.Xs)]
        for layer, w in enumerate(self.weights_list):
            if layer % 2 != 0:
                h_list_recon.append(T.zeros_like(T.dot(h_list_recon[-1], w)))
            else:
                h_list_recon.append((h_t_recon.T[(layer / 2) * self.hidden_size:(layer / 2 + 1) * self.hidden_size]).T)

        # with noise
        _, cost, show_cost = generative_stochastic_network.build_gsn_given_hiddens(self.Xs, h_list, self.weights_list,
                                                                                   self.bias_list, True,
                                                                                   self.noiseless_h1,
                                                                                   self.hidden_add_noise_sigma,
                                                                                   self.input_salt_and_pepper,
                                                                                   self.input_sampling, self.MRG,
                                                                                   self.visible_activation,
                                                                                   self.hidden_activation,
                                                                                   self.walkbacks, self.cost_function,
                                                                                   self.logger)
        # without noise for reconstruction
        x_sample_recon, _, _ = generative_stochastic_network.build_gsn_given_hiddens(self.Xs, h_list_recon,
                                                                                     self.weights_list, self.bias_list,
                                                                                     False, self.noiseless_h1,
                                                                                     self.hidden_add_noise_sigma,
                                                                                     self.input_salt_and_pepper,
                                                                                     self.input_sampling, self.MRG,
                                                                                     self.visible_activation,
                                                                                     self.hidden_activation,
                                                                                     self.walkbacks, self.cost_function,
                                                                                     self.logger)

        updates_train = updates_recurrent
        updates_cost = updates_recurrent

        #############
        #   COSTS   #
        #############
        log.maybeLog(self.logger, '\nCost w.r.t p(X|...) at every step in the graph')
        start_functions_time = time.time()

        # if we are not using Hessian-free training create the normal sgd functions
        if not self.hessian_free:
            gradient = T.grad(cost, self.params)
            gradient_buffer = [theano.shared(numpy.zeros(param.get_value().shape, dtype='float32')) for param in
                               self.params]

            m_gradient = [self.momentum * gb + (cast32(1) - self.momentum) * g for (gb, g) in
                          zip(gradient_buffer, gradient)]
            param_updates = [(param, param - self.learning_rate * mg) for (param, mg) in zip(self.params, m_gradient)]
            gradient_buffer_updates = zip(gradient_buffer, m_gradient)

            updates = OrderedDict(param_updates + gradient_buffer_updates)
            updates_train.update(updates)

            log.maybeLog(self.logger, "rnn-gsn learn...")
            self.f_learn = theano.function(inputs=[self.Xs],
                                           updates=updates_train,
                                           outputs=show_cost,
                                           on_unused_input='warn',
                                           name='rnngsn_f_learn')

            log.maybeLog(self.logger, "rnn-gsn cost...")
            self.f_cost = theano.function(inputs=[self.Xs],
                                          updates=updates_cost,
                                          outputs=show_cost,
                                          on_unused_input='warn',
                                          name='rnngsn_f_cost')

        log.maybeLog(self.logger, "Training/cost functions done.")

        # Denoise some numbers : show number, noisy number, predicted number, reconstructed number
        log.maybeLog(self.logger, "Creating graph for noisy reconstruction function at checkpoints during training.")
        self.f_recon = theano.function(inputs=[self.Xs],
                                       outputs=x_sample_recon[-1],
                                       updates=updates_recurrent_recon,
                                       name='rnngsn_f_recon')

        # a function to add salt and pepper noise
        self.f_noise = theano.function(inputs=[self.X],
                                       outputs=salt_and_pepper(self.X, self.input_salt_and_pepper),
                                       name='rnngsn_f_noise')
        # Sampling functions
        log.maybeLog(self.logger, "Creating sampling function...")
        if self.gsn_layers == 1:
            self.f_sample = theano.function(inputs=[X_sample],
                                            outputs=visible_pX_chain[-1],
                                            name='rnngsn_f_sample_single_layer')
        else:
            # WHY IS THERE A WARNING????
            # because the first odd layers are not used -> directly computed FROM THE EVEN layers
            # unused input = warn
            self.f_sample = theano.function(inputs=self.network_state_input,
                                            outputs=self.network_state_output + visible_pX_chain,
                                            on_unused_input='warn',
                                            name='rnngsn_f_sample')

        log.maybeLog(self.logger, "Done compiling all functions.")
        compilation_time = time.time() - start_functions_time
        # Show the compile time with appropriate easy-to-read units.
        log.maybeLog(self.logger, "Total compilation time took " + make_time_units_string(compilation_time) + ".\n\n")

    def train(self, train_X=None, train_Y=None, valid_X=None, valid_Y=None, test_X=None, test_Y=None,
              is_artificial=False, artificial_sequence=1, continue_training=False):
        log.maybeLog(self.logger, "\nTraining---------\n")
        if train_X is None:
            log.maybeLog(self.logger, "Training using data given during initialization of RNN-GSN.\n")
            train_X = self.train_X
            train_Y = self.train_Y
            if train_X is None:
                log.maybeLog(self.logger, "\nPlease provide a training dataset!\n")
                raise AssertionError("Please provide a training dataset!")
        else:
            log.maybeLog(self.logger, "Training using data provided to training function.\n")
        if valid_X is None:
            valid_X = self.valid_X
            valid_Y = self.valid_Y
        if test_X is None:
            test_X = self.test_X
            test_Y = self.test_Y

        ##########################################################
        # Train the GSN first to get good weights initialization #
        ##########################################################
        if self.train_gsn_first:
            log.maybeLog(self.logger, "\n\n----------Initially training the GSN---------\n\n")
            init_gsn = generative_stochastic_network.GSN(train_X=train_X, valid_X=valid_X, test_X=test_X,
                                                         args=self.gsn_args, logger=self.logger)
            init_gsn.train()

        #############################
        # Save the model parameters #
        #############################
        def save_params_to_file(name, n, gsn_params):
            pass
            print
            'saving parameters...'
            save_path = self.outdir + name + '_params_epoch_' + str(n) + '.pkl'
            f = open(save_path, 'wb')
            try:
                cPickle.dump(gsn_params, f, protocol=cPickle.HIGHEST_PROTOCOL)
            finally:
                f.close()

        def save_params(params):
            values = [param.get_value(borrow=True) for param in params]
            return values

        def restore_params(params, values):
            for i in range(len(params)):
                params[i].set_value(values[i])

        #########################################
        # If we are using Hessian-free training #
        #########################################
        if self.hessian_free:
            pass
        #         gradient_dataset = hf_sequence_dataset([train_X.get_value()], batch_size=None, number_batches=5000)
        #         cg_dataset = hf_sequence_dataset([train_X.get_value()], batch_size=None, number_batches=1000)
        #         valid_dataset = hf_sequence_dataset([valid_X.get_value()], batch_size=None, number_batches=1000)
        #
        #         s = x_samples
        #         costs = [cost, show_cost]
        #         hf_optimizer(params, [Xs], s, costs, u, ua).train(gradient_dataset, cg_dataset, initial_lambda=1.0, preconditioner=True, validation=valid_dataset)

        ################################
        # If we are using SGD training #
        ################################
        else:
            log.maybeLog(self.logger, "\n-----------TRAINING RNN-GSN------------\n")
            # TRAINING
            STOP = False
            counter = 0
            if not continue_training:
                self.learning_rate.set_value(self.init_learn_rate)  # learning rate
            times = []
            best_cost = float('inf')
            best_params = None
            patience = 0

            log.maybeLog(self.logger, ['train X size:', str(train_X.shape.eval())])
            if valid_X is not None:
                log.maybeLog(self.logger, ['valid X size:', str(valid_X.shape.eval())])
            if test_X is not None:
                log.maybeLog(self.logger, ['test X size:', str(test_X.shape.eval())])

            if self.vis_init:
                self.bias_list[0].set_value(logit(numpy.clip(0.9, 0.001, train_X.get_value().mean(axis=0))))

            while not STOP:
                counter += 1
                t = time.time()
                log.maybeAppend(self.logger, [counter, '\t'])

                if is_artificial:
                    data.sequence_mnist_data(train_X, train_Y, valid_X, valid_Y, test_X, test_Y, artificial_sequence,
                                             rng)

                # train
                train_costs = data.apply_cost_function_to_dataset(self.f_learn, train_X, self.batch_size)
                # record it
                log.maybeAppend(self.logger, ['Train:', trunc(train_costs), '\t'])

                # valid
                valid_costs = data.apply_cost_function_to_dataset(self.f_cost, valid_X, self.batch_size)
                # record it
                log.maybeAppend(self.logger, ['Valid:', trunc(valid_costs), '\t'])

                # test
                test_costs = data.apply_cost_function_to_dataset(self.f_cost, test_X, self.batch_size)
                # record it 
                log.maybeAppend(self.logger, ['Test:', trunc(test_costs), '\t'])

                # check for early stopping
                cost = numpy.sum(valid_costs)
                if cost < best_cost * self.early_stop_threshold:
                    patience = 0
                    best_cost = cost
                    # save the parameters that made it the best
                    best_params = save_params(self.params)
                else:
                    patience += 1

                if counter >= self.n_epoch or patience >= self.early_stop_length:
                    STOP = True
                    if best_params is not None:
                        restore_params(self.params, best_params)
                    save_params_to_file('all', counter, self.params)

                timing = time.time() - t
                times.append(timing)

                log.maybeAppend(self.logger, 'time: ' + make_time_units_string(timing) + '\t')

                log.maybeLog(self.logger,
                             'remaining: ' + make_time_units_string((self.n_epoch - counter) * numpy.mean(times)))

                if (counter % self.save_frequency) == 0 or STOP is True:
                    n_examples = 100
                    nums = test_X.get_value(borrow=True)[range(n_examples)]
                    noisy_nums = self.f_noise(test_X.get_value(borrow=True)[range(n_examples)])
                    reconstructions = []
                    for i in xrange(0, len(noisy_nums)):
                        recon = self.f_recon(noisy_nums[max(0, (i + 1) - self.batch_size):i + 1])
                        reconstructions.append(recon)
                    reconstructed = numpy.array(reconstructions)

                    # Concatenate stuff
                    stacked = numpy.vstack([numpy.vstack([nums[i * 10: (i + 1) * 10], noisy_nums[i * 10: (i + 1) * 10],
                                                          reconstructed[i * 10: (i + 1) * 10]]) for i in range(10)])
                    number_reconstruction = PIL.Image.fromarray(
                        tile_raster_images(stacked, (self.root_N_input, self.root_N_input), (10, 30)))

                    number_reconstruction.save(
                        self.outdir + 'rnngsn_number_reconstruction_epoch_' + str(counter) + '.png')

                    # save params
                    save_params_to_file('all', counter, self.params)

                # ANNEAL!
                new_lr = self.learning_rate.get_value() * self.annealing
                self.learning_rate.set_value(new_lr)
