import os
import itertools
import cv2
import time
import codecs
import random
import re
import datetime
import cairocffi as cairo
import editdistance
import imageio
from skimage.transform import resize
import numpy as np
from scipy import ndimage
# import pylab
import matplotlib.pyplot as plt
import matplotlib.pylab as pylab
from keras import backend as K
from keras.layers.convolutional import Conv2D, MaxPooling2D
from keras.layers import Input, Dense, Activation
from keras.layers import Reshape, Lambda
from keras.layers.merge import add, concatenate
from keras.models import Model
from keras.layers.recurrent import GRU
from keras.optimizers import SGD
from keras.utils.data_utils import get_file
from keras.preprocessing import image
import keras.callbacks

OUTPUT_DIR = 'image_ocr'

# character classes and matching regex filter
regex = r'^[a-z ]+$'
alphabet = u'0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz(),\';-?"!#:&*/+. '
alphabet2 = u'abcdefghijklmnopqrstuvwxyz '
data_folder_name = "words"
np.random.seed(54)

###HELPER FUNCTIONS###

#Split an image into text lines
def getLines(image_name):
    ## Convert from RGB to gray
    img = cv2.imread(image_name)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    #cv2.imshow("", gray)
    #cv2.waitKey()
    
    ## Convert to binary image
    th, threshed = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY_INV|cv2.THRESH_OTSU)
    
    
    ## Calculate rotated image
    # pts = cv2.findNonZero(threshed)
    # ret = cv2.minAreaRect(pts)
    # 
    # (cx,cy), (w,h), ang = ret
    # if w>h:
    #     w,h = h,w
    #     ang += 90
    # 
    # M = cv2.getRotationMatrix2D((cx,cy), 50, 1.0)
    # rotated = cv2.warpAffine(threshed, M, (img.shape[1], img.shape[0]))
    rotated = threshed
    # #cv2.imshow("", rotated)
    # #cv2.waitKey()
    
    ## Draw upper and lower lines for each text line
    hist = cv2.reduce(rotated,1, cv2.REDUCE_AVG).reshape(-1)
    
    th = 5
    H,W = img.shape[:2]
    uppers = [y-5 for y in range(H-1) if hist[y]<=th and hist[y+1]>th]
    lowers = [y+5 for y in range(H-1) if hist[y]>th and hist[y+1]<=th]

    
    rotated = gray


    
    minDistance = 20
    count = 0
    croppedImages = []
    for i in range(len(uppers)):
        if(i >= len(lowers)):
            continue
            
        if(uppers[i] < 0):
            uppers[i] = 0
        if(lowers[i] > H):
            lowers[i] = H
        tooClose = False

        if lowers[i] - uppers[i] < minDistance:
            tooClose = True
            #print("too close")
        if not tooClose:
            croppedImages.append(rotated[uppers[i]:lowers[i],:])
            #cv2.line(rotated, (0,uppers[i]), (W, uppers[i]), (255,0,0), 1)
            #cv2.line(rotated, (0,lowers[i]), (W, lowers[i]), (0,255,0), 1)
    if(len(croppedImages)) == 0:
        return np.array([rotated])
    #cv2.imwrite("result.png", rotated)
    return np.array(croppedImages[0:len(croppedImages)])


# this creates larger "blotches" of noise which look
# more realistic than just adding gaussian noise
# assumes greyscale with pixels ranging from 0 to 1
def speckle(img):
    severity = np.random.uniform(0, 0.6)
    blur = ndimage.gaussian_filter(np.random.randn(*img.shape) * severity, 1)
    img_speck = (img + blur)
    img_speck[img_speck > 1] = 1
    img_speck[img_speck <= 0] = 0
    return img_speck


# paints the string in a random location the bounding box
# also uses a random font, a slight random rotation,
# and a random amount of speckle noise
def paint_text(text, w, h, rotate=False, ud=False, multi_fonts=False):
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
    with cairo.Context(surface) as context:
        context.set_source_rgb(1, 1, 1)  # White
        context.paint()
        # this font list works in CentOS 7
        if multi_fonts:
            fonts = ['Century Schoolbook', 'Courier', 'STIX', 'URW Chancery L', 'FreeMono']
            context.select_font_face(np.random.choice(fonts), cairo.FONT_SLANT_NORMAL,
                                     np.random.choice([cairo.FONT_WEIGHT_BOLD, cairo.FONT_WEIGHT_NORMAL]))
        else:
            context.select_font_face('Courier', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        context.set_font_size(25)
        box = context.text_extents(text)
        border_w_h = (4, 4)
        if box[2] > (w - 2 * border_w_h[1]) or box[3] > (h - 2 * border_w_h[0]):
            raise IOError('Could not fit string into image. Max char count is too large for given image width.')

        # teach the RNN translational invariance by
        # fitting text box randomly on canvas, with some room to rotate
        max_shift_x = w - box[2] - border_w_h[0]
        max_shift_y = h - box[3] - border_w_h[1]
        top_left_x = np.random.randint(0, int(max_shift_x))
        if ud:
            top_left_y = np.random.randint(0, int(max_shift_y))
        else:
            top_left_y = h // 2
        context.move_to(top_left_x - int(box[0]), top_left_y - int(box[1]))
        context.set_source_rgb(0, 0, 0)
        context.show_text(text)

    buf = surface.get_data()
    a = np.frombuffer(buf, np.uint8)
    a.shape = (h, w, 4)
    a = a[:, :, 0]  # grab single channel
    a = a.astype(np.float32) / 255
    a = np.expand_dims(a, 0)
    if rotate:
        a = image.random_rotation(a, 3 * (w - top_left_x) / w + 1)
    a = speckle(a)

    return a


def shuffle_mats_or_lists(matrix_list, stop_ind=None):
    ret = []
    assert all([len(i) == len(matrix_list[0]) for i in matrix_list])
    len_val = len(matrix_list[0])
    if stop_ind is None:
        stop_ind = len_val
    assert stop_ind <= len_val

    a = list(range(stop_ind))
    np.random.shuffle(a)
    b = list(range(stop_ind, len_val))
    np.random.shuffle(b)
    a += b
    for mat in matrix_list:
        if isinstance(mat, np.ndarray):
            ret.append(mat[a])
        elif isinstance(mat, list):
            ret.append([mat[i] for i in a])
        else:
            raise TypeError('`shuffle_mats_or_lists` only supports '
                            'numpy.array and list objects.')
    return ret


# Translation of characters to unique integer values
def text_to_labels(text):
    ret = []
    for char in text:
        if(alphabet.find(char) == -1):
            print(char)
        ret.append(alphabet.find(char))
    return ret


# Reverse translation of numerical classes back to characters
def labels_to_text(labels):
    ret = []
    for c in labels:
        if c == len(alphabet):  # CTC Blank
            ret.append("")
        else:
            ret.append(alphabet[c])
    return "".join(ret)


# only a-z and space..probably not to difficult
# to expand to uppercase and symbols

def is_valid_str(in_str):
    search = re.compile(regex, re.UNICODE).search
    return bool(search(in_str))

            
# the actual loss calc occurs here despite it not being
# an internal Keras loss function

def ctc_lambda_func(args):
    y_pred, labels, input_length, label_length = args
    # the 2 is critical here since the first couple outputs of the RNN
    # tend to be garbage:
    y_pred = y_pred[:, 2:, :]
    return K.ctc_batch_cost(labels, y_pred, input_length, label_length)


# For a real OCR application, this should be beam search with a dictionary
# and language model.  For this example, best path is sufficient.

def decode_batch(test_func, word_batch):
    out = test_func([word_batch])[0]
    ret = []
    print(out.shape)
    for j in range(out.shape[0]):
        out_best = list(np.argmax(out[j, 2:], 1))
        out_best = [k for k, g in itertools.groupby(out_best)]
        outstr = labels_to_text(out_best)
        ret.append(outstr)
    return ret
    
class DatasetConfig:
    def __init__(self):
        global data_folder_name
        self.labels_file = "data/" + data_folder_name + ".txt"
        self.data_dir = "data/" + data_folder_name + "/"

def get_dataset_from(iam_dataset_config):
    image_paths = []
    labels = []
    with open(iam_dataset_config.labels_file) as f:
        labeled_data = f.readlines()
    labeled_data = [x.strip() for x in labeled_data]
    for example in labeled_data:
        example_data = example.split()
        if example_data[1] == "ok":
            folder_path = example_data[0].split('-')
            folder_path[0]
            image_path = iam_dataset_config.data_dir + folder_path[0] + "/" + folder_path[0] + "-" + folder_path[1] + "/" + example_data[0] + ".png"
            image_paths.append(image_path)
            label = example_data[-1]
            labels.append(label)
    return image_paths, labels
    

    
### CALLBACK CLASSES ###
# Uses generator functions to supply train/test with
# data. Image renderings are text are created on the fly
# each time with random perturbations

class TextImageGenerator(keras.callbacks.Callback):

    def __init__(self, monogram_file, bigram_file, minibatch_size,
                 img_w, img_h, downsample_factor, val_split,
                 absolute_max_string_len=100, start_epoch=0, data_type="computer", num_words=96000):
        self.num_words = num_words
        self.X_images = []
        self.data_type = data_type
        self.minibatch_size = minibatch_size
        self.epochsPassed = 0
        self.epoch = start_epoch
        self.img_w = img_w
        self.img_h = img_h
        self.monogram_file = monogram_file
        self.bigram_file = bigram_file
        self.downsample_factor = downsample_factor
        self.val_split = val_split
        self.blank_label = self.get_output_size() - 1
        self.absolute_max_string_len = absolute_max_string_len
        self.initialize_data()


    def initialize_data(self):
        global numChanges
        print("Loading initial data, one-time operation")
        (im, l) = get_dataset_from(DatasetConfig())
        print(str(len(im)) + " files found")
        tmp_image_list = []
        tmp_output_list = []
        
        #reshape data
        for i in range(int(self.num_words/numChanges)):
            if i % 100 == 0:
                print(str(i) + "loaded")
            #image = imageio.imread(im[i])
            image = getLines(im[i])[0]

            outputs = l[i].split('|')
            output = ""
            for o in outputs:
                output += o
                output += " "
            output = output[:-1]
            factor = self.img_h/image.shape[0]
            #image = resize(image, (self.img_h, int(image.shape[1]*factor)))
            image = cv2.resize(image, (int(image.shape[1]*factor), self.img_h))
            
            difference = self.img_w-image.shape[1]
            if difference > 0:
                image = np.pad(image, ((0,0),(int(difference/2), int(difference/2))), mode="maximum")
            image = cv2.resize(image, (self.img_w, self.img_h))
            image = np.swapaxes(image, 0, 1)
            th, image = cv2.threshold(image, 127, 255, cv2.THRESH_BINARY|cv2.THRESH_OTSU)
            #image = image / 255
            #add normal image
            tmp_image_list.append(image/255)
            tmp_output_list.append(output)
            #plt.imshow((image/255).T,cmap="Greys_r")
            #plt.show()
            
            #shrink image
            shrinkFactor = random.uniform(.8, .9)
            image = cv2.resize(image, (int(float(self.img_h)*shrinkFactor), int(float(self.img_w)*shrinkFactor)))
            differenceW = self.img_w-image.shape[0]
            differenceH = self.img_h-image.shape[1]
            image = np.pad(image, ((int(differenceW/2),int(differenceW/2)),(int(differenceH/2), int(differenceH/2))), mode="maximum")
            image=cv2.resize(image, (self.img_h, self.img_w))

            #tmp_image_list.append(image)
            #tmp_output_list.append(output)
            #plt.imshow(image.T,cmap="Greys_r")
            #plt.show()
            
            #apply rotation
            th, image = cv2.threshold(image, 127, 255, cv2.THRESH_BINARY_INV|cv2.THRESH_OTSU)
            pts = cv2.findNonZero(image)
            ret = cv2.minAreaRect(pts)
            (cx,cy), (w,h), ang = ret
            if w>h:
                w,h = h,w
                ang += 90      
            randAngle = random.uniform(-2, 2)
            M = cv2.getRotationMatrix2D((cx,cy), randAngle, 1.0)
            image = cv2.warpAffine(image, M, (image.shape[1], image.shape[0]))
            image = cv2.resize(image, (self.img_h, self.img_w))

            th, rotated = cv2.threshold(image, 127, 255, cv2.THRESH_BINARY_INV|cv2.THRESH_OTSU)
            
            #print(rotated.T)
            #add rotated image to list
            tmp_image_list.append(rotated/255)
            tmp_output_list.append(output)
            #plt.imshow(rotated.T,  cmap='Greys_r')
            #plt.show()            

        self.num_words = len(tmp_image_list)
        words_per_epoch = len(tmp_image_list)
        val_split = 0.2
        val_words = int(words_per_epoch * (val_split))
        self.val_split = (words_per_epoch - val_words)
        print("Initial data loaded")
        self.images_list = tmp_image_list
        self.l_list = tmp_output_list
    
    def get_data(self):
        return self.images_list, self.l_list
        
    def get_output_size(self):
        return len(alphabet) + 1

        
    #fills X_images, Y_data, and Y_len
    def build_image_list(self):
        print("Num words:" + str(self.num_words))
        print("Training data size:" + str(self.val_split))
        print("Testing data size:" + str(self.num_words - self.val_split))
        assert self.num_words % self.minibatch_size == 0
        assert (self.val_split * self.num_words) % self.minibatch_size == 0
        self.image_list = [''] * self.num_words
        self.output_list = [''] * self.num_words
        tmp_image_list = []
        tmp_output_list = []
        self.Y_data = np.ones([self.num_words, self.absolute_max_string_len]) * -1
        self.X_images = []
        self.X_text = []
        self.Y_len = [0] * self.num_words
        
        (self.images_list, self.l_list) = shuffle_mats_or_lists([self.images_list, self.l_list], self.val_split)
        (tmp1_image_list, tmp1_output_list) = self.get_data()
        for i in range(self.num_words):
            tmp_image_list.append(tmp1_image_list[i])
            tmp_output_list.append(tmp1_output_list[i])
        if len(tmp_image_list) != self.num_words:
            raise IOError('Could not pull enough words from supplied monogram and bigram files. ')
            
        # interlace to mix up the easy and hard words
        self.image_list[::2] = tmp_image_list[:self.num_words // 2]
        self.image_list[1::2] = tmp_image_list[self.num_words // 2:]
        self.output_list[::2] = tmp_output_list[:self.num_words // 2]
        self.output_list[1::2] = tmp_output_list[self.num_words // 2:]

        for i, line in enumerate(self.output_list):

            if(len(line) != len(text_to_labels(line))):
                print("bad")
            self.Y_len[i] = len(line)
            self.Y_data[i, 0:len(line)] = text_to_labels(line)
            self.X_images.append(self.image_list[i])
            self.X_text.append(line)
            
        self.Y_len = np.expand_dims(np.array(self.Y_len), 1)

        self.cur_val_index = self.val_split

        self.cur_train_index = 0
        self.shuffle_data()
        
    
    #Fills X_text, Y_data, and Y_len
    def build_word_list(self, num_words, max_string_len=None, mono_fraction=0.5):
        print("Num_Words:"+str(num_words), "Max_String_Len:"+str(max_string_len), "Mono_Frac:"+str(mono_fraction), "Img_W:"+str(self.img_w))
        assert max_string_len <= self.absolute_max_string_len
        assert num_words % self.minibatch_size == 0
        assert (self.val_split * num_words) % self.minibatch_size == 0
        self.num_words = num_words
        self.string_list = [''] * self.num_words
        tmp_string_list = []
        self.max_string_len = max_string_len
        self.Y_data = np.ones([self.num_words, self.absolute_max_string_len]) * -1
        self.X_text = []
        self.Y_len = [0] * self.num_words

        # monogram file is sorted by frequency in english speech
        with codecs.open(self.monogram_file, mode='r', encoding='utf-8') as f:
            for line in f:
                if len(tmp_string_list) == int(self.num_words * mono_fraction):
                    break
                word = line.rstrip()
                if max_string_len == -1 or max_string_len is None or len(word) <= max_string_len:
                    tmp_string_list.append(word)

        # bigram file contains common word pairings in english speech
        with codecs.open(self.bigram_file, mode='r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines:
                if len(tmp_string_list) == self.num_words:
                    break
                columns = line.lower().split()
                word = columns[0] + ' ' + columns[1]
                if is_valid_str(word) and \
                        (max_string_len == -1 or max_string_len is None or len(word) <= max_string_len):
                    tmp_string_list.append(word)
        if len(tmp_string_list) != self.num_words:
            raise IOError('Could not pull enough words from supplied monogram and bigram files. ')
        # interlace to mix up the easy and hard words
        self.string_list[::2] = tmp_string_list[:self.num_words // 2]
        self.string_list[1::2] = tmp_string_list[self.num_words // 2:]

        for i, word in enumerate(self.string_list):
            self.Y_len[i] = len(word)
            self.Y_data[i, 0:len(word)] = text_to_labels(word)
            self.X_text.append(word)
        self.Y_len = np.expand_dims(np.array(self.Y_len), 1)

        self.cur_val_index = self.val_split
        self.cur_train_index = 0

    # each time an image is requested from train/val/test, a new random
    # painting of the text is performed
    def get_batch(self, index, size, train):
        # width and height are backwards from typical Keras convention
        # because width is the time dimension when it gets fed into the RNN
        if K.image_data_format() == 'channels_first':
            X_data = np.ones([size, 1, self.img_w, self.img_h])
        else:
            X_data = np.ones([size, self.img_w, self.img_h, 1])

        labels = np.ones([size, self.absolute_max_string_len])
        input_length = np.zeros([size, 1])
        label_length = np.zeros([size, 1])
        source_str = []
        #for every image in the minibatch
        for i in range(size):
            # Mix in some blank inputs.  This seems to be important for
            # achieving translational invariance
            if train and i > size - 4:
                if K.image_data_format() == 'channels_first':
                    X_data[i, 0, 0:self.img_w, :] = self.paint_func('')[0, :, :].T
                else:
                    X_data[i, 0:self.img_w, :, 0] = self.paint_func('',)[0, :, :].T
                labels[i, 0] = self.blank_label
                input_length[i] = self.img_w // self.downsample_factor - 2
                label_length[i] = 1
                source_str.append('')
            else:
                if self.data_type == "handwritten":
                    #Assign X_data to image data
                    if K.image_data_format() == 'channels_first':
                        X_data[i, 0, 0:self.img_w, :] = self.X_images[index+i] 
                    else:
                        X_data[i, 0:self.img_w, :, 0] = self.X_images[index+i]
                
                elif self.data_type == "computer":
                    #Assign X_data to image data
                    if K.image_data_format() == 'channels_first':
                        X_data[i, 0, 0:self.img_w, :] = self.paint_func(self.X_text[index + i])[0, :, :].T
                    else:
                        X_data[i, 0:self.img_w, :, 0] = self.paint_func(self.X_text[index + i])[0, :, :].T
                        
                #Assign labels to the Y_data from image
                labels[i, :] = self.Y_data[index + i]
                input_length[i] = self.img_w // self.downsample_factor - 2
                label_length[i] = self.Y_len[index + i]

                source_str.append(self.X_text[index + i])
        inputs = {'the_input': X_data,
                  'the_labels': labels,
                  'input_length': input_length,
                  'label_length': label_length,
                  'source_str': source_str  # used for visualization only
                  }
        outputs = {'ctc': np.zeros([size])}  # dummy data for dummy loss function
        return (inputs, outputs)
        
    def shuffle_data(self):
        if(self.data_type=="computer"):
            (self.X_text, self.Y_data, self.Y_len) = shuffle_mats_or_lists(
                [self.X_text, self.Y_data, self.Y_len], self.val_split)
        elif self.data_type=="handwritten":
            (self.X_text, self.X_images, self.Y_data, self.Y_len) = shuffle_mats_or_lists([self.X_text,self.X_images, self.Y_data, self.Y_len], self.val_split)
            
    def next_train(self):
        while 1:
            ret = self.get_batch(self.cur_train_index, self.minibatch_size, train=True)
            self.cur_train_index += self.minibatch_size
            #if out of train data
            if self.cur_train_index >= self.val_split:
                #reset train index
                self.cur_train_index = self.cur_train_index % self.minibatch_size
                #shuffle input data
                self.shuffle_data()
            yield ret

    def next_val(self):
        while 1:
            ret = self.get_batch(self.cur_val_index, self.minibatch_size, train=False)
            self.cur_val_index += self.minibatch_size
            if self.cur_val_index >= self.num_words:
                self.cur_val_index = self.val_split + self.cur_val_index % self.minibatch_size
            yield ret

    def on_train_begin(self, logs={}):
        if(self.data_type == "computer"):
            if(self.epoch > 10):
                frac = 1-((self.epoch-10)*.1)
                if(frac < .5):
                    frac = .5
            else:
                frac = 1
            
            self.build_word_list(16000+(self.epoch*800), int(4 + 4*(self.epoch/5)), frac)
            
        elif self.data_type == "handwritten":
            self.build_image_list()
            
        self.paint_func = lambda text: paint_text(text, self.img_w, self.img_h,
                                                  rotate=False, ud=False, multi_fonts=False)

    def on_epoch_begin(self, epoch, logs={}):
        self.epoch = epoch
        # rebind the paint function to implement curriculum learning
        if 3 <= epoch < 6:
            self.paint_func = lambda text: paint_text(text, self.img_w, self.img_h, rotate=False, ud=True, multi_fonts=False)
        elif 6 <= epoch < 9:
            self.paint_func = lambda text: paint_text(text, self.img_w, self.img_h, rotate=False, ud=True, multi_fonts=True)
        elif epoch >= 9:
            self.paint_func = lambda text: paint_text(text, self.img_w, self.img_h, rotate=True, ud=True, multi_fonts=True)
        
        if(self.data_type == "computer"):
            if(self.epochsPassed > 0):
                if(self.epoch > 10):
                    frac = 1-((self.epoch-10)*.1)
                    if(frac < .5):
                        frac = .5
                else:
                    frac = 1
                self.build_word_list(16000+(self.epoch*800), int(4 + 4*(self.epoch/5)), frac)
        elif self.data_type == "handwritten":
            if(self.epochsPassed > 0):
                np.random.seed(epoch)
                self.build_image_list()
                
        self.epochsPassed += 1

#Visual callback for creating png files
class VizCallback(keras.callbacks.Callback):

    def __init__(self, run_name, test_func, text_img_gen, num_display_words=4):
        self.test_func = test_func
        self.output_dir = os.path.join(
            OUTPUT_DIR, run_name)
        self.text_img_gen = text_img_gen
        self.num_display_words = num_display_words
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

    def show_edit_distance(self, num):
        num_left = num
        mean_norm_ed = 0.0
        mean_ed = 0.0
        while num_left > 0:
            word_batch = next(self.text_img_gen)[0]
            num_proc = min(word_batch['the_input'].shape[0], num_left)
            decoded_res = decode_batch(self.test_func, word_batch['the_input'][0:num_proc])
            for j in range(num_proc):
                edit_dist = editdistance.eval(decoded_res[j], word_batch['source_str'][j])
                mean_ed += float(edit_dist)
                mean_norm_ed += float(edit_dist) / len(word_batch['source_str'][j])
            num_left -= num_proc
        mean_norm_ed = mean_norm_ed / num
        mean_ed = mean_ed / num
        print('\nOut of %d samples:  Mean edit distance: %.3f Mean normalized edit distance: %0.3f'
              % (num, mean_ed, mean_norm_ed))

    def on_epoch_end(self, epoch, logs={}):
        print("Epoch ended")
        self.model.save_weights(os.path.join(self.output_dir, 'weights%02d.h5' % (epoch)))
        self.show_edit_distance(256)
        word_batch = next(self.text_img_gen)[0]
        res = decode_batch(self.test_func, word_batch['the_input'][0:self.num_display_words])
        if word_batch['the_input'][0].shape[0] < 256:
            cols = 2
        else:
            cols = 1
        for i in range(self.num_display_words):
            plt.subplot(self.num_display_words // cols, cols, i + 1)
            if K.image_data_format() == 'channels_first':
                the_input = word_batch['the_input'][i, 0, :, :]
            else:
                the_input = word_batch['the_input'][i, :, :, 0]
            plt.imshow(the_input.T, cmap='Greys_r')
            plt.xlabel('Actual = \'%s\'\nGuess = \'%s\'' % (word_batch['source_str'][i], res[i]))
        fig = pylab.gcf()
        fig.set_size_inches(10, 13)
        plt.savefig(os.path.join(self.output_dir, 'e%02d.png' % (epoch)))
        plt.close()
        
def getModel(epoch, img_w):
    return train(epoch, 0, img_w, "", False)
    
#Train model from start_epoch to stop_epoch for image of width img_w
def train(start_epoch, stop_epoch, img_w, data_source, train_mode=True):
    global data_folder_name
    global test_func
    global numChanges
    numChanges=2
    run_name = datetime.datetime.now().strftime('models')
    minibatch_size = 64
    words_per_epoch = 11200*numChanges
    val_split = 0.2
    val_words = int(words_per_epoch * (val_split))
    step_count = (words_per_epoch - val_words) // minibatch_size
    
    # Input Parameters
    img_h = 32
    
    data_type = "handwritten"
    data_folder_name = data_source

    # Network parameters
    conv_filters = 16
    kernel_size = (3, 3)
    pool_size = 2
    time_dense_size = 32
    rnn_size = 512
    
    if K.image_data_format() == 'channels_first':
        input_shape = (1, img_w, img_h)
    else:
        input_shape = (img_w, img_h, 1)
    
    if(train_mode):
        #Download wordlist
        fdir = os.path.dirname(get_file('wordlists.tgz',
                                        origin='http://www.mythic-ai.com/datasets/wordlists.tgz', untar=True))
        
        #Create image data generator from wordlists
        img_gen = TextImageGenerator(monogram_file=os.path.join(fdir, 'wordlist_mono_clean.txt'),
                                    bigram_file=os.path.join(fdir, 'wordlist_bi_clean.txt'),
                                    minibatch_size=minibatch_size,
                                    img_w=img_w,
                                    img_h=img_h,
                                    downsample_factor=(pool_size ** 2),
                                    val_split=step_count * minibatch_size,
                                    start_epoch=start_epoch,
                                    data_type=data_type,
                                    num_words=words_per_epoch
                                    )
                                
    #Begin defining network structure #                            
    act = 'relu'
    input_data = Input(name='the_input', shape=input_shape, dtype='float32')
    
    #Two convolutional-pooling layers
    inner = Conv2D(conv_filters, kernel_size, padding='same',
                   activation=act, kernel_initializer='he_normal',
                   name='conv1')(input_data)
    inner = MaxPooling2D(pool_size=(pool_size, pool_size), name='max1')(inner)
    inner = Conv2D(conv_filters, kernel_size, padding='same',
                   activation=act, kernel_initializer='he_normal',
                   name='conv2')(inner)
    inner = MaxPooling2D(pool_size=(pool_size, pool_size), name='max2')(inner)

    conv_to_rnn_dims = (img_w // (pool_size ** 2), (img_h // (pool_size ** 2)) * conv_filters)
    inner = Reshape(target_shape=conv_to_rnn_dims, name='reshape')(inner)

    #Shrink input size for RNN
    inner = Dense(time_dense_size, activation=act, name='dense1')(inner)

    #Two bi-direction GRU layers
    gru_1 = GRU(rnn_size, return_sequences=True, kernel_initializer='he_normal', name='gru1')(inner)
    gru_1b = GRU(rnn_size, return_sequences=True, go_backwards=True, kernel_initializer='he_normal', name='gru1_b')(inner)
    gru1_merged = add([gru_1, gru_1b])
    gru_2 = GRU(rnn_size, return_sequences=True, kernel_initializer='he_normal', name='gru2')(gru1_merged)
    gru_2b = GRU(rnn_size, return_sequences=True, go_backwards=True, kernel_initializer='he_normal', name='gru2_b')(gru1_merged)

    #Reshape the output of RNN to match output characters
    inner = Dense(len(alphabet) + 1, kernel_initializer='he_normal',
                  name='dense2')(concatenate([gru_2, gru_2b]))
    y_pred = Activation('softmax', name='softmax')(inner)
    mo = Model(inputs=input_data, outputs=y_pred)
    mo.summary()
    
    #Create visual callback with output of model
    test_func = K.function([input_data], [y_pred])
    if(train_mode == False):
        if start_epoch > 0:
            weight_file = os.path.join(OUTPUT_DIR, os.path.join(run_name, 'weights%02d.h5' % (start_epoch - 1)))
            mo.load_weights(weight_file)
            pass
        return test_func

    #Define inputs necessary for CTC loss function
    labels = Input(name='the_labels', shape=[img_gen.absolute_max_string_len], dtype='float32')
    input_length = Input(name='input_length', shape=[1], dtype='int64')
    label_length = Input(name='label_length', shape=[1], dtype='int64')
    
    #Keras doesn't currently support loss funcs with extra parameters
    #so CTC loss is implemented in a lambda layer
    loss_out = Lambda(ctc_lambda_func, output_shape=(1,), name='ctc')([y_pred, labels, input_length, label_length])

    #SGD optimizer
    sgd = SGD(lr=0.02, decay=1e-6, momentum=0.9, nesterov=True, clipnorm=5)

    #Create model
    model = Model(inputs=[input_data, labels, input_length, label_length], outputs=loss_out)
    model.summary()

    #Compile model with SGD optimizer and fake loss function
    model.compile(loss={'ctc': lambda y_true, y_pred: y_pred}, optimizer=sgd)
    if start_epoch > 0:
        weight_file = os.path.join(OUTPUT_DIR, os.path.join(run_name, 'weights%02d.h5' % (start_epoch - 1)))
        model.load_weights(weight_file)
        


    viz_cb = VizCallback(run_name, test_func, img_gen.next_val())
    #Run training sequence
    model.fit_generator(generator=img_gen.next_train(),
                        steps_per_epoch=step_count,
                        epochs=stop_epoch,
                        validation_data=img_gen.next_val(),
                        validation_steps=val_words // minibatch_size,
                    
                        callbacks=[viz_cb, img_gen],
                        initial_epoch=start_epoch)

def runTraining(epochNumber):
    i = 0
    epochsPerTrain = 50;
    while True:
        train(epochNumber+(i*epochsPerTrain), epochNumber+(i*epochsPerTrain)+epochsPerTrain, 1024, "words")
        i += 1

    
def predict(image_name):
    #image parameters
    img_h = 32
    img_w = 1024
    model_name = 7
    #test_func = getModel(model_name, img_w)
    print("Model loaded")
    image_lines = getLines(image_name)
    print(image_lines.shape)
    for image in image_lines:
        factor = img_h/image.shape[0]
        #image = resize(image, (self.img_h, int(image.shape[1]*factor)))
        image = cv2.resize(image, (int(image.shape[1]*factor), img_h))
        
        difference = img_w-image.shape[1]
        if difference > 0:
            image = np.pad(image, ((0,0),(int(difference/2), int(difference/2))), mode="maximum")
        image = cv2.resize(image, (img_w, img_h))
        image = np.swapaxes(image, 0, 1)
        th, image = cv2.threshold(image, 127, 255, cv2.THRESH_BINARY|cv2.THRESH_OTSU)
        #image = image / 255
        #add normal image
        
        image_list = np.reshape(np.array([image/255]), (1, img_w, img_h, 1))

        plt.subplot(1, 1, 1)
        plt.imshow(image_list[0,:,:,0].T, cmap='Greys_r')
        plt.show()
        #print("S:", image_list[0, :, :, 0].shape)
        #decoded_res = decode_batch(test_func, image_list[0:1])
        #plt.title("Guess: " + decoded_res[0])
        #plt.show()
    K.clear_session()
    return decoded_res[0]

#predict("sample.png")