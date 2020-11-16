import numpy as np
import argparse
import os
import gc
import sys
import traceback
import torch
import torch.nn as nn
import torch.optim as optim
import torchvision.transforms as transforms
from torch.nn.modules.distance import PairwiseDistance
from datasets.LFWDataset import LFWDataset
from losses.triplet_loss import TripletLoss
from datasets.TripletLossDataset import TripletFaceDataset
from validate_on_LFW import evaluate_lfw
from plot import plot_roc_lfw, plot_accuracy_lfw, plot_triplet_losses
from tqdm import tqdm
from models.inceptionresnetv2 import InceptionResnetV2Triplet
from models.mobilenetv2 import MobileNetV2Triplet
from models.resnet import (
    Resnet18Triplet,
    Resnet34Triplet,
    Resnet50Triplet,
    Resnet101Triplet,
    Resnet152Triplet
)


parser = argparse.ArgumentParser(description="Training a FaceNet facial recognition model using Triplet Loss.")
parser.add_argument('--dataroot', '-d', type=str, required=True,
                    help="(REQUIRED) Absolute path to the dataset folder"
                    )
parser.add_argument('--lfw', type=str, required=True,
                    help="(REQUIRED) Absolute path to the labeled faces in the wild dataset folder"
                    )
parser.add_argument('--dataset_csv', type=str, default='datasets/vggface2_full.csv',
                    help="Path to the csv file containing the image paths of the training dataset."
                    )
parser.add_argument('--epochs', default=300, type=int,
                    help="Required training epochs (default: 300)"
                    )
parser.add_argument('--model_architecture', type=str, default="resnet18", choices=["resnet18", "resnet34", "resnet50", "resnet101", "resnet152", "inceptionresnetv2", "mobilenetv2"],
                    help="The required model architecture for training: ('resnet18','resnet34', 'resnet50', 'resnet101', 'resnet152', 'inceptionresnetv2', 'mobilenetv2'), (default: 'resnet18')"
                    )
parser.add_argument('--pretrained', default=False, type=bool,
                    help="Download a model pretrained on the ImageNet dataset (Default: False)"
                    )
parser.add_argument('--embedding_dim', default=256, type=int,
                    help="Dimension of the embedding vector (default: 256)"
                    )
parser.add_argument('--num_human_identities_per_batch', default=30, type=int,
                    help="Number of set human identities per generated triplets batch. (Default: 30)."
                    )
parser.add_argument('--batch_size', default=150, type=int,
                    help="Batch size (default: 150)"
                    )
parser.add_argument('--lfw_batch_size', default=150, type=int,
                    help="Batch size for LFW dataset (default: 150)"
                    )
parser.add_argument('--num_generate_triplets_processes', default=0, type=int,
                    help="Number of Python processes to be spawned to generate training triplets per epoch. (Default: 0 (number of all available CPU cores))."
                    )
parser.add_argument('--resume_path', default='',  type=str,
                    help='path to latest model checkpoint: (model_training_checkpoints/model_resnet18_epoch_1.pt file) (default: None)'
                    )
parser.add_argument('--num_workers', default=2, type=int,
                    help="Number of workers for data loaders (default: 2)"
                    )
parser.add_argument('--optimizer', type=str, default="adagrad", choices=["sgd", "adagrad", "rmsprop", "adam"],
                    help="Required optimizer for training the model: ('sgd','adagrad','rmsprop','adam'), (default: 'adagrad')"
                    )
parser.add_argument('--lr', default=0.05, type=float,
                    help="Learning rate for the optimizer (default: 0.05)"
                    )
parser.add_argument('--margin', default=0.2, type=float,
                    help='margin for triplet loss (default: 0.2)'
                    )
parser.add_argument('--image_size', default=224, type=int,
                    help='Input image size (default: 224 (224x224), must be 299x299 for Inception-ResNet-V2)'
                    )
parser.add_argument('--use_semihard_negatives', default=True, type=bool,
                    help="If True: use semihard negative triplet selection. Else: use hard negative triplet selection (Default: True)"
                    )
parser.add_argument('--training_triplets_path', default=None, type=str,
                    help="Path to training triplets numpy file in 'datasets' folder to skip training triplet generation step."
                    )
args = parser.parse_args()


def set_model_architecture(model_architecture, pretrained, embedding_dimension):
    if model_architecture == "resnet18":
        model = Resnet18Triplet(
            embedding_dimension=embedding_dimension,
            pretrained=pretrained
        )
    elif model_architecture == "resnet34":
        model = Resnet34Triplet(
            embedding_dimension=embedding_dimension,
            pretrained=pretrained
        )
    elif model_architecture == "resnet50":
        model = Resnet50Triplet(
            embedding_dimension=embedding_dimension,
            pretrained=pretrained
        )
    elif model_architecture == "resnet101":
        model = Resnet101Triplet(
            embedding_dimension=embedding_dimension,
            pretrained=pretrained
        )
    elif model_architecture == "resnet152":
        model = Resnet152Triplet(
            embedding_dimension=embedding_dimension,
            pretrained=pretrained
        )
    elif model_architecture == "inceptionresnetv2":
        model = InceptionResnetV2Triplet(
            embedding_dimension=embedding_dimension,
            pretrained=pretrained
        )
    elif model_architecture == "mobilenetv2":
        model = MobileNetV2Triplet(
            embedding_dimension=embedding_dimension,
            pretrained=pretrained
        )
    print("Using {} model architecture.".format(model_architecture))

    return model


def set_model_gpu_mode(model):
    flag_train_gpu = torch.cuda.is_available()
    flag_train_multi_gpu = False

    if flag_train_gpu and torch.cuda.device_count() > 1:
        model = nn.DataParallel(model)
        model.cuda()
        flag_train_multi_gpu = True
        print('Using multi-gpu training.')

    elif flag_train_gpu and torch.cuda.device_count() == 1:
        model.cuda()
        print('Using single-gpu training.')

    return model, flag_train_multi_gpu


def set_optimizer(optimizer, model, learning_rate):
    if optimizer == "sgd":
        optimizer_model = optim.SGD(
            params=model.parameters(),
            lr=learning_rate,
            momentum=0.9,
            dampening=0,
            nesterov=False
        )

    elif optimizer == "adagrad":
        optimizer_model = optim.Adagrad(
            params=model.parameters(),
            lr=learning_rate,
            lr_decay=0,
            initial_accumulator_value=0.1,
            eps=1e-10
        )

    elif optimizer == "rmsprop":
        optimizer_model = optim.RMSprop(
            params=model.parameters(),
            lr=learning_rate,
            alpha=0.99,
            eps=1e-08,
            momentum=0,
            centered=False
        )

    elif optimizer == "adam":
        optimizer_model = optim.Adam(
            params=model.parameters(),
            lr=learning_rate,
            betas=(0.9, 0.999),
            eps=1e-08,
            amsgrad=False
        )

    return optimizer_model


def validate_lfw(model, lfw_dataloader, model_architecture, epoch, epochs):
    model.eval()
    with torch.no_grad():
        l2_distance = PairwiseDistance(p=2)
        distances, labels = [], []

        print("Validating on LFW! ...")
        progress_bar = enumerate(tqdm(lfw_dataloader))

        for batch_index, (data_a, data_b, label) in progress_bar:
            data_a = data_a.cuda()
            data_b = data_b.cuda()

            output_a, output_b = model(data_a), model(data_b)
            distance = l2_distance.forward(output_a, output_b)  # Euclidean distance

            distances.append(distance.cpu().detach().numpy())
            labels.append(label.cpu().detach().numpy())

        labels = np.array([sublabel for label in labels for sublabel in label])
        distances = np.array([subdist for distance in distances for subdist in distance])

        true_positive_rate, false_positive_rate, precision, recall, accuracy, roc_auc, best_distances, \
        tar, far = evaluate_lfw(
            distances=distances,
            labels=labels,
            far_target=1e-3
        )
        # Print statistics and add to log
        print("Accuracy on LFW: {:.4f}+-{:.4f}\tPrecision {:.4f}+-{:.4f}\tRecall {:.4f}+-{:.4f}\t"
              "ROC Area Under Curve: {:.4f}\tBest distance threshold: {:.2f}+-{:.2f}\t"
              "TAR: {:.4f}+-{:.4f} @ FAR: {:.4f}".format(
                    np.mean(accuracy),
                    np.std(accuracy),
                    np.mean(precision),
                    np.std(precision),
                    np.mean(recall),
                    np.std(recall),
                    roc_auc,
                    np.mean(best_distances),
                    np.std(best_distances),
                    np.mean(tar),
                    np.std(tar),
                    np.mean(far)
                )
        )
        with open('logs/lfw_{}_log_triplet.txt'.format(model_architecture), 'a') as f:
            val_list = [
                epoch + 1,
                np.mean(accuracy),
                np.std(accuracy),
                np.mean(precision),
                np.std(precision),
                np.mean(recall),
                np.std(recall),
                roc_auc,
                np.mean(best_distances),
                np.std(best_distances),
                np.mean(tar)
            ]
            log = '\t'.join(str(value) for value in val_list)
            f.writelines(log + '\n')

    try:
        # Plot ROC curve
        plot_roc_lfw(
            false_positive_rate=false_positive_rate,
            true_positive_rate=true_positive_rate,
            figure_name="plots/roc_plots/roc_{}_epoch_{}_triplet.png".format(model_architecture, epoch + 1)
        )
        # Plot LFW accuracies plot
        plot_accuracy_lfw(
            log_dir="logs/lfw_{}_log_triplet.txt".format(model_architecture),
            epochs=epochs,
            figure_name="plots/lfw_accuracies_{}_triplet.png".format(model_architecture)
        )
    except Exception as e:
        print(e)

    return best_distances


def forward_pass(imgs, model, optimizer_model, batch_idx, optimizer,
                 learning_rate, batch_size, use_cpu=False):
    # If CUDA is Out of Memory, do a forward pass on CPU (model and optimizer are already loaded to CPU)
    if use_cpu:
        flag_use_cpu = True
        torch.cuda.empty_cache()

        imgs = imgs.cpu()
        embeddings = model(imgs)
        embeddings = embeddings.cpu()

        # Split the embeddings into Anchor, Positive, and Negative embeddings
        anc_embeddings = embeddings[:batch_size]
        pos_embeddings = embeddings[batch_size: batch_size * 2]
        neg_embeddings = embeddings[batch_size * 2:]

        # Free some memory
        del imgs, embeddings
        gc.collect()

        return anc_embeddings, pos_embeddings, neg_embeddings, model, optimizer_model, flag_use_cpu

    # Forward pass on CUDA
    #  Model already loaded to CUDA
    else:
        try:
            flag_use_cpu = False

            imgs = imgs.cuda()
            embeddings = model(imgs)
            embeddings = embeddings.cpu()

            # Split the embeddings into Anchor, Positive, and Negative embeddings
            anc_embeddings = embeddings[:batch_size]
            pos_embeddings = embeddings[batch_size: batch_size * 2]
            neg_embeddings = embeddings[batch_size * 2:]

            # Free some memory
            del imgs, embeddings
            gc.collect()

            return anc_embeddings, pos_embeddings, neg_embeddings, model, optimizer_model, flag_use_cpu

        # CUDA Out of Memory Exception Handling
        #  Load model and optimizer to cpu then retry forward pass
        except RuntimeError as e:
            # Inspired by:
            # https://github.com/pytorch/fairseq/blob/50a671f78d0c8de0392f924180db72ac9b41b801/fairseq/trainer.py#L284
            if "out of memory" in str(e):
                # Print original exception stack traceback
                exc_info = sys.exc_info()
                traceback.print_exception(*exc_info)

                print("\nCUDA Out of Memory at iteration {}. Retrying iteration on CPU!".format(batch_idx))

                # According to https://github.com/pytorch/pytorch/issues/2830#issuecomment-336183179
                #  In order for the optimizer to keep training the model after changing to a different type or device,
                #  optimizers have to be recreated, 'load_state_dict' can be used to restore the state from a
                #  previous copy. As such, the optimizer state dict will be saved first and then reloaded when
                #  the model's device is changed.
                optimizer_model.zero_grad()

                torch.save(
                    optimizer_model.state_dict(),
                    'model_training_checkpoints/out_of_memory_optimizer_checkpoint/optimizer_checkpoint.pt'
                )

                # Load model to CPU
                model.cpu()

                optimizer_model = set_optimizer(
                    optimizer=optimizer,
                    model=model,
                    learning_rate=learning_rate
                )

                optimizer_model.load_state_dict(
                    torch.load(
                        'model_training_checkpoints/out_of_memory_optimizer_checkpoint/optimizer_checkpoint.pt'
                    )
                )

                # Copied from https://github.com/pytorch/pytorch/issues/2830#issuecomment-336194949
                # No optimizer.cpu() available, this is the way to make an optimizer loaded with cuda tensors load
                #  with cpu tensors
                for state in optimizer_model.state.values():
                    for k, v in state.items():
                        if torch.is_tensor(v):
                            state[k] = v.cpu()

                return forward_pass(
                    imgs=imgs,
                    model=model,
                    optimizer_model=optimizer_model,
                    batch_idx=batch_idx,
                    optimizer=optimizer,
                    learning_rate=learning_rate,
                    batch_size=batch_size,
                    use_cpu=True
                )
            else:
                raise e


def train_triplet(start_epoch, end_epoch, epochs, train_dataloader, lfw_dataloader, lfw_validation_epoch_interval,
                  model, model_architecture, optimizer_model, embedding_dimension, batch_size, margin,
                  flag_train_multi_gpu, optimizer, learning_rate, use_semihard_negatives):

    for epoch in range(start_epoch, end_epoch):
        flag_validate_lfw = (epoch + 1) % lfw_validation_epoch_interval == 0 or (epoch + 1) % epochs == 0
        triplet_loss_sum = 0
        num_valid_training_triplets = 0
        l2_distance = PairwiseDistance(p=2)

        # Training pass
        model.train()
        progress_bar = enumerate(tqdm(train_dataloader))

        for batch_idx, (batch_sample) in progress_bar:
            # Skip last iteration to avoid the problem of having different number of tensors while calculating
            #  pairwise distances (sizes of tensors must be the same for pairwise distance calculation)
            if batch_idx + 1 == len(train_dataloader):
                continue

            # Forward pass - compute embeddings
            anc_imgs = batch_sample['anc_img']
            pos_imgs = batch_sample['pos_img']
            neg_imgs = batch_sample['neg_img']

            # Concatenate the input images into one tensor because doing multiple forward passes would create
            #  weird GPU memory allocation behaviours later on during training which would cause GPU Out of Memory
            #  issues
            all_imgs = torch.cat((anc_imgs, pos_imgs, neg_imgs))  # Must be a tuple of Torch Tensors

            anc_embeddings, pos_embeddings, neg_embeddings, model, optimizer_model, flag_use_cpu = forward_pass(
                imgs=all_imgs,
                model=model,
                optimizer_model=optimizer_model,
                batch_idx=batch_idx,
                optimizer=optimizer,
                learning_rate=learning_rate,
                batch_size=batch_size,
                use_cpu=False
            )

            pos_dists = l2_distance.forward(anc_embeddings, pos_embeddings)
            neg_dists = l2_distance.forward(anc_embeddings, neg_embeddings)

            if use_semihard_negatives:
                # Semi-Hard Negative triplet selection
                #  (negative_distance - positive_distance < margin) AND (positive_distance < negative_distance)
                #   Based on: https://github.com/davidsandberg/facenet/blob/master/src/train_tripletloss.py#L295

                first_condition = (neg_dists - pos_dists < margin).cpu().numpy().flatten()
                second_condition = (pos_dists < neg_dists).cpu().numpy().flatten()
                all = (np.logical_and(first_condition, second_condition))

                semihard_negative_triplets = np.where(all == 1)
                if len(semihard_negative_triplets[0]) == 0:
                    continue

                anc_valid_embeddings = anc_embeddings[semihard_negative_triplets]
                pos_valid_embeddings = pos_embeddings[semihard_negative_triplets]
                neg_valid_embeddings = neg_embeddings[semihard_negative_triplets]

                del anc_embeddings, pos_embeddings, neg_embeddings, pos_dists, neg_dists
                gc.collect()

            else:
                # Hard Negative triplet selection
                #  (negative_distance - positive_distance < margin)
                #   Based on: https://github.com/davidsandberg/facenet/blob/master/src/train_tripletloss.py#L296

                all = (neg_dists - pos_dists < margin).cpu().numpy().flatten()

                hard_negative_triplets = np.where(all == 1)
                if len(hard_negative_triplets[0]) == 0:
                    continue

                anc_valid_embeddings = anc_embeddings[hard_negative_triplets]
                pos_valid_embeddings = pos_embeddings[hard_negative_triplets]
                neg_valid_embeddings = neg_embeddings[hard_negative_triplets]

                del anc_embeddings, pos_embeddings, neg_embeddings, pos_dists, neg_dists
                gc.collect()

            # Calculate triplet loss
            triplet_loss = TripletLoss(margin=margin).forward(
                anchor=anc_valid_embeddings,
                positive=pos_valid_embeddings,
                negative=neg_valid_embeddings
            )

            # Calculating loss and number of triplets that met the triplet selection method during the epoch
            triplet_loss_sum += triplet_loss.item()
            num_valid_training_triplets += len(anc_valid_embeddings)

            # Backward pass
            optimizer_model.zero_grad()
            triplet_loss.backward()
            optimizer_model.step()

            # Load model and optimizer back to GPU if CUDA Out of Memory Exception occurred and model and optimizer
            #  were switched to CPU
            if flag_use_cpu:
                # According to https://github.com/pytorch/pytorch/issues/2830#issuecomment-336183179
                #  In order for the optimizer to keep training the model after changing to a different type or device,
                #  optimizers have to be recreated, 'load_state_dict' can be used to restore the state from a
                #  previous copy. As such, the optimizer state dict will be saved first and then reloaded when
                #  the model's device is changed.
                torch.cuda.empty_cache()

                # Print number of valid triplets (troubleshooting out of memory causes)
                print("Number of valid triplets during OOM iteration = {}".format(
                        len(anc_valid_embeddings)
                    )
                )

                torch.save(
                    optimizer_model.state_dict(),
                    'model_training_checkpoints/out_of_memory_optimizer_checkpoint/optimizer_checkpoint.pt'
                )

                # Load back to CUDA
                model.cuda()

                optimizer_model = set_optimizer(
                    optimizer=optimizer,
                    model=model,
                    learning_rate=learning_rate
                )

                optimizer_model.load_state_dict(
                    torch.load(
                        'model_training_checkpoints/out_of_memory_optimizer_checkpoint/optimizer_checkpoint.pt'
                    )
                )

                # Copied from https://github.com/pytorch/pytorch/issues/2830#issuecomment-336194949
                # No optimizer.cuda() available, this is the way to make an optimizer loaded with cpu tensors load
                #  with cuda tensors.
                for state in optimizer_model.state.values():
                    for k, v in state.items():
                        if torch.is_tensor(v):
                            state[k] = v.cuda()

            # Clear some memory at end of training iteration
            del triplet_loss, anc_valid_embeddings, pos_valid_embeddings, neg_valid_embeddings
            gc.collect()

        # Model only trains on triplets that fit the triplet selection method
        avg_triplet_loss = 0 if (num_valid_training_triplets == 0) else triplet_loss_sum / num_valid_training_triplets

        # Print training statistics and add to log
        print('Epoch {}:\tAverage Triplet Loss: {:.4f}\tNumber of valid training triplets in epoch: {}'.format(
                epoch + 1,
                avg_triplet_loss,
                num_valid_training_triplets
            )
        )

        with open('logs/{}_log_triplet.txt'.format(model_architecture), 'a') as f:
            val_list = [
                epoch + 1,
                avg_triplet_loss,
                num_valid_training_triplets
            ]
            log = '\t'.join(str(value) for value in val_list)
            f.writelines(log + '\n')

        try:
            # Plot Triplet losses plot
            plot_triplet_losses(
                log_dir="logs/{}_log_triplet.txt".format(model_architecture),
                epochs=epochs,
                figure_name="plots/triplet_losses_{}.png".format(model_architecture)
            )
        except Exception as e:
            print(e)

        # Evaluation pass on LFW dataset
        if flag_validate_lfw:
            best_distances = validate_lfw(
                model=model,
                lfw_dataloader=lfw_dataloader,
                model_architecture=model_architecture,
                epoch=epoch,
                epochs=epochs
            )

        # Save model checkpoint
        state = {
            'epoch': epoch + 1,
            'embedding_dimension': embedding_dimension,
            'batch_size_training': batch_size,
            'model_state_dict': model.state_dict(),
            'model_architecture': model_architecture,
            'optimizer_model_state_dict': optimizer_model.state_dict()
        }

        # For storing data parallel model's state dictionary without 'module' parameter
        if flag_train_multi_gpu:
            state['model_state_dict'] = model.module.state_dict()

        # For storing best euclidean distance threshold during LFW validation
        if flag_validate_lfw:
            state['best_distance_threshold'] = np.mean(best_distances)

        # Save model checkpoint
        torch.save(state, 'model_training_checkpoints/model_{}_triplet_epoch_{}.pt'.format(
                model_architecture,
                epoch + 1
            )
        )


def main():
    dataroot = args.dataroot
    lfw_dataroot = args.lfw
    dataset_csv = args.dataset_csv
    epochs = args.epochs
    model_architecture = args.model_architecture
    pretrained = args.pretrained
    embedding_dimension = args.embedding_dim
    num_human_identities_per_batch = args.num_human_identities_per_batch
    batch_size = args.batch_size
    lfw_batch_size = args.lfw_batch_size
    num_generate_triplets_processes = args.num_generate_triplets_processes
    resume_path = args.resume_path
    num_workers = args.num_workers
    optimizer = args.optimizer
    learning_rate = args.lr
    margin = args.margin
    image_size = args.image_size
    use_semihard_negatives = args.use_semihard_negatives
    training_triplets_path = args.training_triplets_path
    start_epoch = 0

    # Define image data pre-processing transforms
    #   ToTensor() normalizes pixel values between [0, 1]
    #   Normalize(mean=[0.6068, 0.4517, 0.3800], std=[0.2492, 0.2173, 0.2082]) normalizes pixel values to be mean
    #    of zero and standard deviation of 1 according to the calculated VGGFace2 with tightly-cropped faces dataset RGB
    #    channels' mean and std values by calculate_vggface2_rgb_mean_std.py in 'datasets' folder.
    data_transforms = transforms.Compose([
        transforms.Resize(size=image_size),
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(degrees=5),
        transforms.ToTensor(),
        transforms.Normalize(
            mean=[0.6068, 0.4517, 0.3800],
            std=[0.2492, 0.2173, 0.2082]
        )
    ])

    lfw_transforms = transforms.Compose([
        transforms.Resize(size=image_size),
        transforms.ToTensor(),
        transforms.Normalize(
            mean=[0.6068, 0.4517, 0.3800],
            std=[0.2492, 0.2173, 0.2082]
        )
    ])

    # Set dataloaders
    train_dataloader = torch.utils.data.DataLoader(
        dataset=TripletFaceDataset(
            root_dir=dataroot,
            csv_name=dataset_csv,
            num_triplets=num_triplets_train,
            num_generate_triplets_processes=num_generate_triplets_processes,
            training_triplets_path=training_triplets_path,
            num_human_identities_per_batch=num_human_identities_per_batch,
            triplet_batch_size=batch_size,
            transform=data_transforms
        ),
        batch_size=batch_size,
        num_workers=num_workers,
        shuffle=False  # Shuffling for triplets with set amount of human identities per batch is not required
    )

    lfw_dataloader = torch.utils.data.DataLoader(
        dataset=LFWDataset(
            dir=lfw_dataroot,
            pairs_path='datasets/LFW_pairs.txt',
            transform=lfw_transforms
        ),
        batch_size=lfw_batch_size,
        num_workers=num_workers,
        shuffle=False
    )

    # Instantiate model
    model = set_model_architecture(
        model_architecture=model_architecture,
        pretrained=pretrained,
        embedding_dimension=embedding_dimension
    )

    # Load model to GPU or multiple GPUs if available
    model, flag_train_multi_gpu = set_model_gpu_mode(model)

    # Set optimizer
    optimizer_model = set_optimizer(
        optimizer=optimizer,
        model=model,
        learning_rate=learning_rate
    )

    # Resume from a model checkpoint
    if resume_path:
        if os.path.isfile(resume_path):
            print("Loading checkpoint {} ...".format(resume_path))

            checkpoint = torch.load(resume_path)
            start_epoch = checkpoint['epoch']

            optimizer_model.load_state_dict(checkpoint['optimizer_model_state_dict'])

            # In order to load state dict for optimizers correctly, model has to be loaded to gpu first
            if flag_train_multi_gpu:
                model.module.load_state_dict(checkpoint['model_state_dict'])
            else:
                model.load_state_dict(checkpoint['model_state_dict'])

            print("Checkpoint loaded: start epoch from checkpoint = {}".format(start_epoch))
        else:
            print("WARNING: No checkpoint found at {}!\nTraining from scratch.".format(resume_path))

    if use_semihard_negatives:
        print("Using Semi-Hard negative triplet selection!")
    else:
        print("Using Hard negative triplet selection!")

    # Start Training loop
    print("Training using triplet loss starting for {} epochs:\n".format(epochs - start_epoch))

    start_epoch = start_epoch
    end_epoch = start_epoch + epochs

    # Start training model using Triplet Loss
    train_triplet(
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        epochs=epochs,
        train_dataloader=train_dataloader,
        lfw_dataloader=lfw_dataloader,
        lfw_validation_epoch_interval=lfw_validation_epoch_interval,
        model=model,
        model_architecture=model_architecture,
        optimizer_model=optimizer_model,
        embedding_dimension=embedding_dimension,
        batch_size=batch_size,
        margin=margin,
        flag_train_multi_gpu=flag_train_multi_gpu,
        optimizer=optimizer,
        learning_rate=learning_rate,
        use_semihard_negatives=use_semihard_negatives
    )


if __name__ == '__main__':
    main()
