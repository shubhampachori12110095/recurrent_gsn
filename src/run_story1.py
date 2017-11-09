import argparse

import Story1


def main():
    parser = argparse.ArgumentParser()
    # Add options here

    # network settings
    parser.add_argument('--layers', type=int, default=3)  # number of hidden layers
    parser.add_argument('--walkbacks', type=int, default=5)  # number of walkbacks
    parser.add_argument('--hidden_size', type=int, default=1000)
    parser.add_argument('--hidden_act', type=str, default='tanh')
    parser.add_argument('--visible_act', type=str, default='sigmoid')
    parser.add_argument('--sequence_window_size', type=int,
                        default=2)  # how much history to consider (including the current input x)

    # training
    parser.add_argument('--cost_funct', type=str, default='binary_crossentropy')  # the cost function for training
    parser.add_argument('--n_epoch', type=int, default=30)
    parser.add_argument('--batch_size', type=int, default=100)
    parser.add_argument('--save_frequency', type=int, default=10)  # number of epochs between parameters being saved
    parser.add_argument('--early_stop_threshold', type=float, default=0.9995)  # 0.9995
    parser.add_argument('--early_stop_length', type=int, default=30)
    parser.add_argument('--max_iterations', type=int, default=10)

    # noise
    parser.add_argument('--hidden_add_noise_sigma', type=float, default=2)
    parser.add_argument('--input_salt_and_pepper', type=float, default=0.4)  # default=0.4

    # hyper parameters
    parser.add_argument('--learning_rate', type=float, default=0.25)
    parser.add_argument('--momentum', type=float, default=0.5)
    parser.add_argument('--annealing', type=float, default=0.995)
    parser.add_argument('--regularize_weight', type=float, default=0)

    # data
    parser.add_argument('--dataset', type=str, default='MNIST_4')
    parser.add_argument('--data_path', type=str, default='../data/')
    parser.add_argument('--classes', type=int, default=10)

    # argparse does not deal with booleans
    parser.add_argument('--vis_init', type=int, default=0)
    parser.add_argument('--noiseless_h1', type=int, default=1)
    parser.add_argument('--input_sampling', type=int, default=1)
    parser.add_argument('--test_model', type=int, default=0)
    parser.add_argument('--continue_training', type=int, default=0)  # default=0

    args = parser.parse_args()

    # RUN STORY 1
    Story1.experiment(args, '../outputs/model_4/')
    # args.dataset = "MNIST_2"
    # Story1.experiment(args, '../outputs/model_1/')
    # args.dataset = "MNIST_3"
    # Story1.experiment(args, '../outputs/model_1/')


if __name__ == '__main__':
    main()
