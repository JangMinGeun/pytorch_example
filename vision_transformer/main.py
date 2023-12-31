import argparse
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import torchvision
from torchvision.transforms import Compose, ToTensor, Resize
from torch import optim
import numpy as np
from torch.hub import tqdm
import sys

import schedule
import time
path = '/home/shjeong/deepops/workloads/examples/slurm/examples/vision_transformer'
log_collect = None
class PatchExtractor(nn.Module):
    def __init__(self, patch_size=16):
        super().__init__()
        self.patch_size = patch_size

    def forward(self, input_data):
        batch_size, channels, height, width = input_data.size()
        assert height % self.patch_size == 0 and width % self.patch_size == 0, \
            f"Input height ({height}) and width ({width}) must be divisible by patch size ({self.patch_size})"

        num_patches_h = height // self.patch_size
        num_patches_w = width // self.patch_size
        num_patches = num_patches_h * num_patches_w

        patches = input_data.unfold(2, self.patch_size, self.patch_size). \
            unfold(3, self.patch_size, self.patch_size). \
            permute(0, 2, 3, 1, 4, 5). \
            contiguous(). \
            view(batch_size, num_patches, -1)

        # Expected shape of a patch on default settings is (4, 196, 768)

        return patches


class InputEmbedding(nn.Module):

    def __init__(self, args):
        super(InputEmbedding, self).__init__()
        self.patch_size = args.patch_size
        self.n_channels = args.n_channels
        self.latent_size = args.latent_size
        use_cuda = not args.no_cuda and torch.cuda.is_available()
        self.device = torch.device("cuda" if use_cuda else "cpu")
        self.batch_size = args.batch_size
        self.input_size = self.patch_size * self.patch_size * self.n_channels

        # Linear projection
        self.LinearProjection = nn.Linear(self.input_size, self.latent_size)
        # Class token
        self.class_token = nn.Parameter(torch.randn(self.batch_size, 1, self.latent_size)).to(self.device)
        # Positional embedding
        self.pos_embedding = nn.Parameter(torch.randn(self.batch_size, 1, self.latent_size)).to(self.device)

    def forward(self, input_data):
        input_data = input_data.to(self.device)
        # Patchifying the Image
        patchify = PatchExtractor(patch_size=self.patch_size)
        patches = patchify(input_data)

        linear_projection = self.LinearProjection(patches).to(self.device)
        b, n, _ = linear_projection.shape
        linear_projection = torch.cat((self.class_token, linear_projection), dim=1)
        pos_embed = self.pos_embedding[:, :n + 1, :]
        linear_projection += pos_embed

        return linear_projection


class EncoderBlock(nn.Module):

    def __init__(self, args):
        super(EncoderBlock, self).__init__()

        self.latent_size = args.latent_size
        self.num_heads = args.num_heads
        self.dropout = args.dropout
        self.norm = nn.LayerNorm(self.latent_size)
        self.attention = nn.MultiheadAttention(self.latent_size, self.num_heads, dropout=self.dropout)
        self.enc_MLP = nn.Sequential(
            nn.Linear(self.latent_size, self.latent_size * 4),
            nn.GELU(),
            nn.Dropout(self.dropout),
            nn.Linear(self.latent_size * 4, self.latent_size),
            nn.Dropout(self.dropout)
        )

    def forward(self, emb_patches):
        first_norm = self.norm(emb_patches)
        attention_out = self.attention(first_norm, first_norm, first_norm)[0]
        first_added = attention_out + emb_patches
        second_norm = self.norm(first_added)
        mlp_out = self.enc_MLP(second_norm)
        output = mlp_out + first_added

        return output


class ViT(nn.Module):
    def __init__(self, args):
        super(ViT, self).__init__()

        self.num_encoders = args.num_encoders
        self.latent_size = args.latent_size
        self.num_classes = args.num_classes
        self.dropout = args.dropout

        self.embedding = InputEmbedding(args)
        # Encoder Stack
        self.encoders = nn.ModuleList([EncoderBlock(args) for _ in range(self.num_encoders)])
        self.MLPHead = nn.Sequential(
            nn.LayerNorm(self.latent_size),
            nn.Linear(self.latent_size, self.latent_size),
            nn.Linear(self.latent_size, self.num_classes),
        )

    def forward(self, test_input):
        enc_output = self.embedding(test_input)
        for enc_layer in self.encoders:
            enc_output = enc_layer(enc_output)

        class_token_embed = enc_output[:, 0]
        return self.MLPHead(class_token_embed)


class TrainEval:

    def __init__(self, args, model, train_dataloader, val_dataloader, optimizer, criterion, log_collect, device):
        self.model = model
        self.train_dataloader = train_dataloader
        self.val_dataloader = val_dataloader
        self.optimizer = optimizer
        self.criterion = criterion
        self.epoch = args.epochs
        self.device = device
        self.args = args
        self.log_collect = log_collect

    def train_fn(self, current_epoch):
        self.model.train()
        total_loss = 0.0
        tk = tqdm(self.train_dataloader, desc="EPOCH" + "[TRAIN]" + str(current_epoch + 1) + "/" + str(self.epoch), disable=True)
        # tk = tqdm(self.train_dataloader)
        self.log_collect.change_epoch(current_epoch + 1) #######################################
        for t, data in enumerate(tk):
            self.log_collect.change_iteration(t + 1) #################
            images, labels = data
            images, labels = images.to(self.device), labels.to(self.device)
            self.optimizer.zero_grad()
            logits = self.model(images)
            loss = self.criterion(logits, labels)
            loss.backward()
            self.optimizer.step()

            total_loss += loss.item()
            tk.set_postfix({"Loss": "%6f" % float(total_loss / (t + 1))})
            if self.args.dry_run:
                break

        return total_loss / len(self.train_dataloader)

    def eval_fn(self, current_epoch):
        self.model.eval()
        total_loss = 0.0
        tk = tqdm(self.val_dataloader, desc="EPOCH" + "[VALID]" + str(current_epoch + 1) + "/" + str(self.epoch), disable=True)
        # tk = tqdm(self.train_dataloader)
        for t, data in enumerate(tk):
            images, labels = data
            images, labels = images.to(self.device), labels.to(self.device)

            logits = self.model(images)
            loss = self.criterion(logits, labels)

            total_loss += loss.item()
            tk.set_postfix({"Loss": "%6f" % float(total_loss / (t + 1))})
            if self.args.dry_run:
                break

        return total_loss / len(self.val_dataloader)

    def train(self):
        best_valid_loss = np.inf
        best_train_loss = np.inf
        for i in range(self.epoch):
            train_loss = self.train_fn(i)
            # val_loss = self.eval_fn(i)

            # if val_loss < best_valid_loss:
            #     torch.save(self.model.state_dict(), "best-weights.pt")
            #     print("Saved Best Weights")
            #     best_valid_loss = val_loss
            #     best_train_loss = train_loss
        print(f"Training Loss : {best_train_loss}")
        # print(f"Valid Loss : {best_valid_loss}")

    '''
        On default settings:
        
        Training Loss : 2.3081023390197752
        Valid Loss : 2.302861615943909
        
        However, this score is not competitive compared to the 
        high results in the original paper, which were achieved 
        through pre-training on JFT-300M dataset, then fine-tuning 
        it on the target dataset. To improve the model quality 
        without pre-training, we could try training for more epochs, 
        using more Transformer layers, resizing images or changing 
        patch size,
    '''

class JobLogging:
    def __init__(self, batch_size, total_epoch, total_iteration):
        import socket
        self.total_epoch = total_epoch
        self.current_epoch = 1
        self.total_iteration = total_iteration
        self.current_iteration = 1
        self.gpu_memory = 0
        self.gpu_memory2 = 0
        self.gpu_usage = 0
        self.batch_size = batch_size
        self.job_name = 'vision_transformer'
        self.hostname = socket.gethostname()
        self.gpu = torch.cuda.get_device_name(torch.cuda.current_device())
        file = path+'/out.txt'
        f = open(file, 'w').close()

    def logging(self):
        import nvidia_smi

        nvidia_smi.nvmlInit()
        deviceCount = nvidia_smi.nvmlDeviceGetCount()
        handle = nvidia_smi.nvmlDeviceGetHandleByIndex(torch.cuda.current_device())
        info = nvidia_smi.nvmlDeviceGetMemoryInfo(handle)
        res = nvidia_smi.nvmlDeviceGetUtilizationRates(handle)
        self.gpu_memory = "{:.3f}".format(100 * (info.used / info.total))
        self.gpu_memory2 = res.memory
        self.gpu_usage = res.gpu
        nvidia_smi.nvmlShutdown()

        file = path+'/out.txt'
        f = open(file, 'a')
        hostname = 'server : ' + self.hostname
        job_name = 'job : ' + self.job_name
        gpu = 'gpu : ' + self.gpu
        gpu_memory = "gpu_memory : " + str(self.gpu_memory) + "%"
        gpu_memory2 = "gpu_memory2 :" + str(self.gpu_memory2) + "%"
        gpu_usage = "gpu_usage : " + str(self.gpu_usage) + "%"

        batch_size = "batch_size : " + str(self.batch_size)

        total_epoch = 'total_epoch : ' + str(self.total_epoch)
        current_epoch = 'current_epoch : ' + str(self.current_epoch)

        total_iteration = 'total_iteration : ' + str(self.total_iteration)
        current_iteration = 'current_iteration: ' + str(self.current_iteration) + '\n'

        data = '%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s' \
            %(hostname, job_name, gpu, gpu_memory, gpu_memory2, gpu_usage, batch_size, total_epoch, current_epoch, total_iteration, current_iteration)

        f.write(data)
        f.close()
    
    def change_epoch(self, epoch):
        self.current_epoch = epoch
    def change_iteration(self, iteration):
        self.current_iteration = iteration
    
def start_schedule():
    import os
    import signal
    while True:
        schedule.run_pending()
        time.sleep(5)
    #     break
    # time.sleep(6)
    # schedule.run_pending()
    # os.kill(os.getpid(), signal.SIGUSR1)

def logger():
    log_collect.logging()



def main():
    import threading
    parser = argparse.ArgumentParser(description='Vision Transformer in PyTorch')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--patch-size', type=int, default=16,
                        help='patch size for images (default : 16)')
    parser.add_argument('--latent-size', type=int, default=768,
                        help='latent size (default : 768)')
    parser.add_argument('--n-channels', type=int, default=3,
                        help='number of channels in images (default : 3 for RGB)')
    parser.add_argument('--num-heads', type=int, default=12,
                        help='(default : 16)')
    parser.add_argument('--num-encoders', type=int, default=12,
                        help='number of encoders (default : 12)')
    parser.add_argument('--dropout', type=int, default=0.1,
                        help='dropout value (default : 0.1)')
    parser.add_argument('--img-size', type=int, default=224,
                        help='image size to be reshaped to (default : 224')
    parser.add_argument('--num-classes', type=int, default=10,
                        help='number of classes in dataset (default : 10 for CIFAR10)')
    parser.add_argument('--epochs', type=int, default=10,
                        help='number of epochs (default : 10)')
    parser.add_argument('--lr', type=float, default=1e-2,
                        help='base learning rate (default : 0.01)')
    parser.add_argument('--weight-decay', type=int, default=3e-2,
                        help='weight decay value (default : 0.03)')
    parser.add_argument('--batch-size', type=int, default=4,
                        help='batch size (default : 4)')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='quickly check a single pass')
    args = parser.parse_args()

    use_cuda = not args.no_cuda and torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    transforms = Compose([
        Resize((args.img_size, args.img_size)),
        ToTensor()
    ])
    train_data = torchvision.datasets.CIFAR10(root='./dataset', train=True, download=True, transform=transforms)
    valid_data = torchvision.datasets.CIFAR10(root='./dataset', train=False, download=True, transform=transforms)
    train_loader = DataLoader(train_data, batch_size=args.batch_size, shuffle=True)
    valid_loader = DataLoader(valid_data, batch_size=args.batch_size, shuffle=True)

    

    model = ViT(args).to(device)
    log_collect = JobLogging(args.batch_size, args.epochs, len(train_loader)) ####################

    optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    criterion = nn.CrossEntropyLoss()

    s = sys.stdin.readline()

    schedule.every(10).seconds.do(log_collect.logging)
    schedule_thread = threading.Thread(target= start_schedule, daemon=True)
    schedule_thread.start()
    TrainEval(args, model, train_loader, valid_loader, optimizer, criterion, log_collect, device).train()


if __name__ == "__main__":
    main()

