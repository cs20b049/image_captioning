# -*- coding: utf-8 -*-
"""image_caption_rnn.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1gIBhZm8-zQO2jmNtYimDD2-rfsjKV7Fl
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
import torchvision.transforms as transforms
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import Dataset, DataLoader
import numpy as np
from collections import Counter
import nltk
from PIL import Image
import matplotlib.image as mpimg
import matplotlib.pyplot as plt
import os
import sys
import time
!pip3 install evaluate
import evaluate
import random

# Ensure NLTK tokenizers are available
nltk.download('punkt')

# from google.colab import drive
# drive.mount('/content/drive')

#NetVLAD is wholely taken from  https://github.com/lyakaap/NetVLAD-pytorch/blob/master/netvlad.py
class NetVLAD(nn.Module):
    """NetVLAD layer implementation"""

    def __init__(self, num_clusters=64, dim=128, alpha=100.0,
                 normalize_input=True):
        """
        Args:
            num_clusters : int
                The number of clusters
            dim : int
                Dimension of descriptors
            alpha : float
                Parameter of initialization. Larger value is harder assignment.
            normalize_input : bool
                If true, descriptor-wise L2 normalization is applied to input.
        """
        super(NetVLAD, self).__init__()
        self.num_clusters = num_clusters
        self.dim = dim
        self.alpha = alpha
        self.normalize_input = normalize_input
        self.conv = nn.Conv2d(dim, num_clusters, kernel_size=(1, 1), bias=True)
        self.centroids = nn.Parameter(torch.rand(num_clusters, dim))
        self._init_params()

    def _init_params(self):
        self.conv.weight = nn.Parameter(
            (2.0 * self.alpha * self.centroids).unsqueeze(-1).unsqueeze(-1)
        )
        self.conv.bias = nn.Parameter(
            - self.alpha * self.centroids.norm(dim=1)
        )

    def forward(self, x):
        N, C = x.shape[:2]

        if self.normalize_input:
            x = F.normalize(x, p=2, dim=1)  # across descriptor dim

        # soft-assignment
        soft_assign = self.conv(x).view(N, self.num_clusters, -1)
        soft_assign = F.softmax(soft_assign, dim=1)

        x_flatten = x.view(N, C, -1)

        # calculate residuals to each clusters
        residual = x_flatten.expand(self.num_clusters, -1, -1, -1).permute(1, 0, 2, 3) - \
            self.centroids.expand(x_flatten.size(-1), -1, -1).permute(1, 2, 0).unsqueeze(0)
        residual *= soft_assign.unsqueeze(2)
        vlad = residual.sum(dim=-1)

        vlad = F.normalize(vlad, p=2, dim=2)  # intra-normalization
        vlad = vlad.view(x.size(0), -1)  # flatten
        vlad = F.normalize(vlad, p=2, dim=1)  # L2 normalize

        return vlad


def get_netvlad_feature(feature,netvlad):
    with torch.no_grad():
        netvlad_vector = netvlad(feature)
    return netvlad_vector

class RNNDecoder(nn.Module):
	def __init__(self, embed_size, hidden_size, vocab_size, num_clusters, cnn_output_dim):
		super(RNNDecoder, self).__init__()
		self.linear_transform = nn.Linear(num_clusters * cnn_output_dim, embed_size)
		self.embed = nn.Embedding(vocab_size, embed_size)
		self.drop = nn.Dropout(p=0.25)
		self.rnn = nn.LSTM(embed_size, hidden_size, num_layers=1, batch_first=True)
		self.linear = nn.Linear(hidden_size, vocab_size)

	def forward(self, features, captions):
		embeddings = self.embed(captions)
		features = self.linear_transform(features)
		features = features.unsqueeze(1)
		inputs = torch.cat((features, embeddings[:, :-1,:]), dim=1)
		inputs = self.drop(inputs)
		hiddens, _ = self.rnn(inputs)
		outputs = self.linear(hiddens)
		return outputs

	def generate_caption(self, features, vocab, max_length=20):
		result_caption = []
		states = None
		inputs = self.linear_transform(features).unsqueeze(1)  # (batch_size, 1, embed_size)

		for _ in range(max_length):
			hiddens, states = self.rnn(inputs, states)  # hiddens: (batch_size, 1, hidden_size)
			output = self.linear(hiddens.squeeze(1))  # output: (batch_size, vocab_size)
			predicted = output.argmax(1)  # predicted: (batch_size)
			result_caption.append(predicted.item())
			inputs = self.embed(predicted).unsqueeze(1)  # inputs: (batch_size, 1, embed_size)

			if vocab.itos[predicted.item()] == '<EOS>':
				break

		return [vocab.itos[idx] for idx in result_caption]

class Vocabulary:
    def __init__(self, freq_threshold=5):
        self.freq_threshold = freq_threshold
        self.itos = {0: "<PAD>", 1: "<SOS>", 2: "<EOS>", 3: "<UNK>"}
        self.stoi = {v: k for k, v in self.itos.items()}

    def __len__(self):
        return len(self.itos)

    @staticmethod
    def tokenizer_eng(text):
        return nltk.tokenize.word_tokenize(text.lower())

    def build_vocabulary(self, sentence_list):
        frequencies = Counter()
        idx = 4

        for sentence in sentence_list:
            for word in self.tokenizer_eng(sentence):
                frequencies[word] += 1

                if frequencies[word] == self.freq_threshold:
                    self.stoi[word] = idx
                    self.itos[idx] = word
                    idx += 1

    def numericalize(self, text):
        tokenized_text = self.tokenizer_eng(text)

        return [self.stoi.get(word, self.stoi["<UNK>"]) for word in tokenized_text]

class ImageCaptionDataset(Dataset):
    def __init__(self, captions, vocab, tensor_dir):
        self.tensor_dir = tensor_dir
        self.captions_list = list(captions.items())
        self.vocab = vocab

    def __len__(self):
        return len(self.captions_list)

    def __getitem__(self, idx):
        image_id,caption = self.captions_list[idx]
        image_id=image_id[0:-2]
        tensor_path = os.path.join(self.tensor_dir, image_id + '.pt')
        image_tensor = torch.load(tensor_path)

        numericalized_caption = [self.vocab.stoi["<SOS>"]]
        numericalized_caption += self.vocab.numericalize(caption)
        numericalized_caption.append(self.vocab.stoi["<EOS>"])

        return image_tensor, torch.tensor(numericalized_caption)

def give_train_data(path_to_captions='image_captioning/caption_data/captions.txt', path_to_image_names='image_captioning/caption_data/image_names.txt'):
	captions={}

	image_names=[]
	with open(path_to_image_names, 'r') as file:
		for line in file:
			image_names.append(line.strip())
	image_names=set(image_names)

	with open(path_to_captions,'r') as file:
		for line in file:
			segs=line.split('\t')
			key=segs[0]
			value=segs[1].strip()
			if key[0 :-2] in image_names:
				captions[key]=value

	return image_names,captions

def give_test_data(path_to_captions='image_captioning/caption_data/captions.txt', path_to_image_names='image_captioning/caption_data/image_names.txt'):
	train_image=[]
	with open(path_to_image_names, 'r') as file:
		for line in file:
			train_image.append(line.strip())
	train_image=set(train_image)

	all_captions={}
	all_names=[]

	with open(path_to_captions) as file:
		for line in file:
			segs=line.split('\t')
			key=segs[0][0:-2]
			value=segs[1].strip()
			if key not in all_captions :
				all_captions[key]=[value]
			else :
				all_captions[key].append(value)
			all_names.append(key)
	all_names=list(set(all_names))
	random.shuffle(all_names)

	# print(len(all_names),all_names[0])

	cnt=0
	test_images=[]
	for key in all_names:
		if key not in train_image:
			test_images.append(key)
			cnt+=1
		if cnt==200:
			break

	return test_images,all_captions


def collate_fn(batch):
	images, captions = zip(*batch)
	images = torch.stack(images, 0)
	captions = pad_sequence(captions, batch_first=True, padding_value=0)
	return images, captions

def preprocess_and_save_images(image_dir, save_dir, transform, image_names):
	if not os.path.exists(save_dir):
		os.makedirs(save_dir)
	i=0
	for image_name in image_names:
		save_path = os.path.join(save_dir, image_name + '.pt')
		if os.path.isfile(save_path):
			continue
		i+=1
		image_path = os.path.join(image_dir, image_name)
		image = Image.open(image_path).convert('RGB')
		image_tensor = transform(image)
		torch.save(image_tensor, save_path)
		if i%200==0:
			print(i,'images tensors are processed')

# Set device to GPU if available
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def generate_caption(image_path, transform, get_feature_map, get_netvlad_features, netvlad, decoder, vocab, max_length=20):
	image = Image.open(image_path).convert('RGB')
	image = transform(image).unsqueeze(0).to(device)
	features = get_feature_map(image_path)
	netvlad_vector = get_netvlad_features(features,netvlad)
	caption = decoder.generate_caption(netvlad_vector, vocab, max_length)
	return ' '.join(caption)

def train(get_feature_map,netvlad,decoder,dataloader,criterion,optimizer,vocab_size,num_epochs):
	netvlad.train()
	decoder.train()

	cnt=0
	start_time = time.time()
	for epoch in range(num_epochs):
		for images, captions in dataloader:
			cnt+=1
			images = images.to(device)
			captions = captions.to(device)

			# Forward pass
			features = get_feature_map(images)
			netvlad_vectors = netvlad(features)
			outputs = decoder(netvlad_vectors, captions)

			# Compute loss
			loss = criterion(outputs.view(-1, vocab_size), captions.view(-1))

			# Backpropagation
			optimizer.zero_grad()
			loss.backward()
			optimizer.step()

			if cnt%100==0:
				print(cnt,'batches are done')
		print(f"Epoch [{epoch+1}/{num_epochs}], Loss: {loss.item():.4f}")

	end_time = time.time()
	print(f"Training completed in {end_time-start_time} seconds")

def test(all_captions,test_names,generate_caption,transform, get_feature_map, get_netvlad_features, netvlad, decoder, vocab):
	decoder.eval()

	avg_bleu_score=0
	cnt=0
	for image in test_names:
		cnt+=1
		ref=all_captions[image]
		image_dir = 'image_captioning/Images'
		image_path = os.path.join(image_dir,image)
		pred = generate_caption(image_path, transform, get_feature_map, get_netvlad_features, netvlad, decoder, vocab, max_length=20)
		bleu = evaluate.load("bleu")
		max_val=0
		for i in range(5):
			max_val=max(max_val,bleu.compute(references=[ref[i]],predictions=[pred[5:-5]])['bleu'])
		avg_bleu_score+=max_val

		if cnt%50==0:
			print(f'{cnt}/ 200 pictures have been completed for bleu scores ')
	avg_bleu_score/=len(test_names)
	return avg_bleu_score

train_names, captions = give_train_data()
test_names, all_captions = give_test_data()

vocab = Vocabulary(freq_threshold=6)
vocab.build_vocabulary(list(captions.values()))
vocab_size = len(vocab)
embed_size = 256
hidden_size = 512
print(len(captions),vocab_size)



image_dir = 'image_captioning/Images'
save_dir = 'image_captioning/Tensors'

transform = transforms.Compose([transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
])

preprocess_and_save_images(image_dir, save_dir, transform,train_names)

tensor_dir = 'image_captioning/Tensors'
dataset = ImageCaptionDataset(captions, vocab, tensor_dir)
dataloader = DataLoader(dataset, batch_size=50, shuffle=True, collate_fn=collate_fn)

# Load a pretrained CNN model
cnn = models.resnet50(pretrained=True)
modules = list(cnn.children())[:-2]  # Remove the fully connected layers
cnn = nn.Sequential(*modules).to(device)

cnn.eval()

# Initialize NetVLAD and RNN decoder
num_clusters = 64
dim = 2048
netvlad = NetVLAD(num_clusters=num_clusters, dim=dim).to(device)
decoder = RNNDecoder(embed_size, hidden_size, vocab_size, num_clusters, dim).to(device)
# netvlad.load_state_dict(torch.load('image_captioning/netvlad_lstm.pt'))
# decoder.load_state_dict(torch.load('image_captioning/decoder_lstm.pt'))


# Loss function and optimizer
criterion = nn.CrossEntropyLoss()
optimizer = torch.optim.Adam(decoder.parameters(), lr=0.001)

# Function to extract feature maps using the CNN
def get_feature_map(image):
    with torch.no_grad():
        feature_map = cnn(image)
    return feature_map

train(get_feature_map, netvlad, decoder, dataloader, criterion, optimizer, vocab_size, num_epochs=40)

train_bleu_data=list(train_names)[0:200]
random.shuffle(train_bleu_data)
test_bleu_score=test(all_captions, test_names, generate_caption, transform, get_feature_map, get_netvlad_feature, netvlad, decoder, vocab)
train_bleu_score=test(all_captions, train_bleu_data, generate_caption, transform, get_feature_map, get_netvlad_feature, netvlad, decoder, vocab)
print(f"Average BLEU Score on train data and test data is {train_bleu_score} and {test_bleu_score} respectively .")


save_netvlad_path = 'image_captioning/netvlad_lstm.pt'
save_decoder_path = 'image_captioning/decoder_lstm.pt'
torch.save(netvlad.state_dict(), save_netvlad_path)
torch.save(decoder.state_dict(), save_decoder_path)

def bleu_of_image(image_id,all_captions):
    image_dir = 'image_captioning/Images'
    image_path =os.path.join(image_dir,image_id)
    img = mpimg.imread(image_path)
    plt.imshow(img)
    plt.axis('off')  # Hide axes
    plt.show()
    pred = generate_caption(image_path, transform, get_feature_map, get_netvlad_feature, netvlad, decoder, vocab)
    print("Generated Caption:", pred)

    ref=all_captions[image_id]
    print('These are all the given captions for the image')
    for i in range(5):
        print(ref[i])
    bleu = evaluate.load("bleu")
    max_val=0
    for i in range(5):
        max_val=max(max_val,bleu.compute(references=[ref[i]],predictions=[pred[5:-5]])['bleu'])
    print(f"BLEU Score for this image is {max_val} !")

# bleu_of_image('2346629210_8d6668d22d.jpg',all_captions)

