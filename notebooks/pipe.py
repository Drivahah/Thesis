# import the libraries
import os
import pandas as pd
import torch
import argparse
import numpy as np
# from sentence_transformers import SentenceTransformer
# from transformers import AutoTokenizer, AutoModel
# from tensorflow.keras import layers
# from tensorflow.keras.models import Model
# from tensorflow.keras.layers import Input
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.pipeline import Pipeline
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import GridSearchCV, cross_val_score, StratifiedKFold
from sklearn.compose import ColumnTransformer
import joblib
from imblearn.pipeline import Pipeline as ImbPipeline
from imblearn.over_sampling import SMOTE, ADASYN
from transformers import T5Tokenizer, T5EncoderModel, XLNetTokenizer, XLNetModel
# from torch.optim.lr_scheduler import StepLR
# from torch.utils.data import TensorDataset, DataLoader
import logging
from transformers import AdamW
# import ast
from datetime import datetime

# Set workiiing directory to file location
abspath = os.path.abspath(__file__)
dname = os.path.dirname(abspath)
os.chdir(dname)

# Print start in a file
with open('A.txt', 'a') as f:
    print('Start', file=f)

# parse the command line arguments
parser = argparse.ArgumentParser()
parser.add_argument('--embedder', type=str, choices=['prott5', 'protxlnet'], default='prott5', help='Embedder to use')
parser.add_argument('--fine_tune', action='store_true', help='Whether to fine-tune the embedder')
parser.add_argument('--oversampling', type=str, choices=['smote', 'adasyn', 'none'], default='none', help='Oversampling technique to use for imbalanced data')
parser.add_argument('--estimator', type=str, choices=['logreg', 'rf', 'transformer'], default='logreg', help='Final estimator to use')
# parser.add_argument('--load_embedder', action='store_true', help='Whether to load pre-trained parameters for the embedder')
# parser.add_argument('--load_estimator', action='store_true', help='Whether to load pre-trained parameters for the final estimator')
parser.add_argument('--train', action='store_true', help='Whether to train the whole model')
parser.add_argument('--grid_search', action='store_true', help='Whether to perform grid search for the pipeline')
parser.add_argument('--param_grid', type=str, default='{}', help='Parameter grid for the grid search as a string')
parser.add_argument('--predict', type=str, help='Specify the pre-trained model to use to predict on the given data')
parser.add_argument('--epochs', type=int, default=10, help='Number of epochs for fine-tuning the embedder')
parser.add_argument('--steps', type=int, default=10, help='Number of steps to train the embedder before freezing the parameters')
parser.add_argument('--lr_embedder', type=float, default=0.01, help='Learning rate for the embedder')
parser.add_argument('--lr_fine_tuned', type=float, default=0.001, help='Learning rate for the fine-tuned model')
parser.add_argument('--batch_size', type=int, default=3, help='Batch size for embedding the data')
parser.add_argument('--device', type=str, default='cuda:0', help='Device to use for the models')
parser.add_argument('--debug', action='store_true', help='Whether to enable debug logging')
parser.add_argument('--quick', action='store_true', help='Run only on the first batch')
parser.add_argument('--SP', type=str, choices=['parallel', 'sequential'], default='sequential', help='Whether to embed the sequences in parallel or sequentially')
args = parser.parse_args()

# Print args parsed in a file
with open('A.txt', 'a') as f:
    print(args, file=f)

def load_data(df_path, quick, splits=dict()):
    if not os.path.exists(df_path):
        raise FileNotFoundError(f'{df_path} does not exist')
    # If quick is True, load the test df
    if quick:
        df_dir = os.path.join('..', 'data', 'interim')
        df = pd.read_pickle(os.path.join(df_dir, 'test_df.pkl'))
    else:
        df = pd.read_pickle(df_path)
        # Sort by length of 'sequence_phage' and 'sequence_k12' columns
        # It reduces the number of padding residues needed
        df.sort_values(by=['sequence_phage', 'sequence_k12'], key=lambda x: x.str.len(), ascending=False, inplace=True)

        # Reset the index to make it contiguous
        df.reset_index(drop=True, inplace=True)

    # Return X, y columns as numpy arrays
    return df[['sequence_phage', 'sequence_k12']].values, df['pair'].values


# define the custom embedder classes
class BaseEmbedder(BaseEstimator, TransformerMixin):
    def __init__(self, model_name, device='cuda:0', fine_tune=False, num_epochs=1, num_steps=0, learning_rate=1e-3,org='phage'):
        # Set device and check if available
        if device == 'cuda:0' and not torch.cuda.is_available():
            raise RuntimeError('CUDA is not available')
            
        self.device = device
        self.fine_tune = fine_tune
        self.num_epochs = num_epochs
        self.num_steps = num_steps
        self.learning_rate = learning_rate
        self.model_name = model_name
        # Load tokenizer and model
        self.load_model_and_tokenizer()
        # only GPUs support half-precision currently; if you want to run on CPU use full-precision (not recommended, much slower)
        self.model.full() if self.device=='cpu' else self.model.half()
        self.model.eval() # set model to eval mode, we don't want to train it

        self.org = org
        
    def load_model_and_tokenizer(self):
        raise NotImplementedError
    
    def fit(self, X, y=None):
        self.device = torch.device(self.device)

        if self.fine_tune:
            # Convert X to a list if it is not already a list
            if not isinstance(X, list):
                X = X.tolist()
            # Flatten X to be a list of strings
            X = [item[0] for item in X]
            # Print X shape and first three rows to file
            with open('A.txt', 'a') as f:
                print('X shape', len(X), len(X[0]), file=f)
                print('X first three rows', X[:3], file=f)
            self.model.train() # set model to training mode
            optimizer = AdamW(self.model.parameters(), lr=self.learning_rate)
            if self.num_steps == 0:
                self.num_steps = len(X)
            batch_size = len(X) // self.num_steps
            for epoch in range(self.num_epochs):
                for step in range(self.num_steps):
                    # get the batch
                    batch = X[step*batch_size:(step+1)*batch_size]
                    # encode the batch
                    token_encoding = self.tokenizer.batch_encode_plus(batch, add_special_tokens=True, padding="longest")
                    input_ids = torch.tensor(token_encoding['input_ids']).to(self.device)
                    attention_mask = torch.tensor(token_encoding['attention_mask']).to(self.device)
                    outputs = self.model(input_ids, attention_mask=attention_mask)
                    loss = outputs.loss
                    loss.backward()
                    optimizer.step()
                    optimizer.zero_grad()
            self.model.eval() # set model back to eval mode
        return self

    def transform(self, X, batch_size=1):
        with open('A.txt', 'a') as f:
            print('org', self.org, file=f)
        self.device = torch.device(self.device)
        # Convert X to a list if it is not already a list
        if not isinstance(X, list):
            X = X.tolist()

        # Flatten X to be a list of strings
        X = [item[0] for item in X]

        # Print X shape and first row to file
        with open('A.txt', 'a') as f:
            print('X shape', len(X), len(X[0]), file=f)
            print('X first row', X[0], file=f)  
        # initialize an empty list to store the embeddings
        embeddings_list = []
        # loop over the batches
        for i in range(0, len(X), batch_size):
            # Get the batch
            batch = X[i:i+batch_size]
            with open('A.txt', 'a') as f:
                print('batch = C[i:i+batch_size]\n', batch, file=f)
            # Each batch is a list of lists, so we need to flatten it
            # batch = [item for sublist in batch for item in sublist]
            # with open('A.txt', 'a') as f:
            #     print('batch = [item for sublist in batch for item in sublist]\n', batch, file=f)

            # encode the batch
            token_encoding = self.tokenizer.batch_encode_plus(batch, add_special_tokens=True, padding="longest")
            with open('A.txt', 'a') as f:
                print('token_encoding\n', token_encoding, file=f)
            input_ids = torch.tensor(token_encoding['input_ids']).to(self.device)
            with open('A.txt', 'a') as f:
                print('input_ids\n', input_ids, file=f)
            attention_mask = torch.tensor(token_encoding['attention_mask']).to(self.device)
            with open('A.txt', 'a') as f:
                print('attention_mask\n', attention_mask, file=f)
            with torch.no_grad():
                embeddings = self.model(input_ids, attention_mask)
                # print the shape of the embeddings to file
                with open('A.txt', 'a') as f:
                    print('embeddings = self.model', file=f)
                    # print('embeddings_shape: ', embeddings.shape, file=f)
                    print('embeddings\n', embeddings, file=f)

                for batch_index in range(len(batch)):
                    emb = embeddings.last_hidden_state[batch_index, :len(batch[batch_index])]
                    emb = emb.mean(dim=0).detach().cpu().numpy().squeeze()
                    # Wrap the 1D array into a 2D array
                    emb = emb.reshape(1, -1)
                    # print the shape of the embeddings to file
                    with open('A.txt', 'a') as f:
                        # print('emb_shape: ', emb.shape, file=f)
                        print('emb\n', emb, file=f)
                    # append the embeddings to the list
                    embeddings_list.append(emb)
            with open('A.txt', 'a') as f:
                print('embeddings_list\n', embeddings_list, file=f)
        # concatenate the list to an array
        embeddings_array = np.concatenate(embeddings_list, axis=0)
        with open('A.txt', 'a') as f:
            print('embeddings_array\n', embeddings_array, file=f)
        return embeddings_array

class ProtT5Embedder(BaseEmbedder):
    def load_model_and_tokenizer(self):
        self.tokenizer = T5Tokenizer.from_pretrained(self.model_name, do_lower_case=False)
        self.model = T5EncoderModel.from_pretrained(self.model_name).to(self.device)

class ProtXLNetEmbedder(BaseEmbedder):
    def load_model_and_tokenizer(self):
        self.tokenizer = XLNetTokenizer.from_pretrained(self.model_name, do_lower_case=False)
        self.model = XLNetModel.from_pretrained(self.model_name).to(self.device)

class SequentialEmbedder(BaseEstimator, TransformerMixin):
    def __init__(self, embedder_phage, embedder_bacteria):
        self.embedder_phage = embedder_phage
        self.embedder_bacteria = embedder_bacteria

    def fit(self, X, y=None):
        # Fit the first embedder
        self.embedder_phage.fit(X[:, 0], y)
        # Fit the second embedder
        self.embedder_bacteria.fit(X[:, 1], y)
        return self

    def transform(self, X):
        # Transform the first column
        embeddings_phage = self.embedder_phage.transform(X[:, 0])
        # Transform the second column
        embeddings_bacteria = self.embedder_bacteria.transform(X[:, 1])
        # Concatenate the results
        return np.concatenate([embeddings_phage, embeddings_bacteria], axis=1)


# Print class defined in a file
with open('A.txt', 'a') as f:
    print('Classes defined', file=f)

# create the pipeline
if args.embedder == 'prott5':
    embedder_phage = ProtT5Embedder('Rostlab/prot_t5_xl_half_uniref50-enc', fine_tune=args.fine_tune, device=args.device)
    embedder_bacteria = ProtT5Embedder('Rostlab/prot_t5_xl_half_uniref50-enc', fine_tune=args.fine_tune, device=args.device, org='bacteria')
elif args.embedder == 'protxlnet':
    embedder_phage = ProtXLNetEmbedder('Rostlab/prot_xlnet', fine_tune=args.fine_tune, device=args.device)
    embedder_bacteria = ProtXLNetEmbedder('Rostlab/prot_xlnet', fine_tune=args.fine_tune, device=args.device, org='bacteria')

# Get the column indices for the features you want to transform
sequence_phage_col_index = 0
sequence_k12_col_index = 1

if args.SP == 'parallel':
    column_transformer = ColumnTransformer(
        transformers=[
            ('embedder_phage', embedder_phage, [sequence_phage_col_index]),
            ('embedder_bacteria', embedder_bacteria, [sequence_k12_col_index]),
        ],
        remainder='drop'  # drop any columns not specified in transformers
    )
elif args.SP == 'sequential':
    column_transformer = SequentialEmbedder(embedder_phage, embedder_bacteria)

# if args.load_embedder:
#     # load pre-trained parameters for the embedder
#     embedder.load_state_dict(torch.load('embedder.pth'))
#     embedder.eval()

if args.estimator == 'logreg':
    estimator = LogisticRegression()
elif args.estimator == 'rf':
    estimator = RandomForestClassifier()
# elif args.estimator == 'transformer':
#     # use a transformer model as the final estimator
#     estimator = TransformerClassifier(device=args.device)

# if args.load_estimator:
#     # load pre-trained parameters for the final estimator
#     estimator = joblib.load(args.estimator + '.pkl')

if args.oversampling == 'smote':
    # use SMOTE for imbalanced data
    pipe = ImbPipeline([
        ('column_transformer', column_transformer),
        ('smote', SMOTE()),
        ('estimator', estimator)
    ])
elif args.oversampling == 'adasyn':
    # use ADASYN for imbalanced data
    pipe = ImbPipeline([
        ('column_transformer', column_transformer),
        ('adasyn', ADASYN()),
        ('estimator', estimator)
    ])
else:
    # do not use any oversampling technique
    pipe = Pipeline([
        ('column_transformer', column_transformer),
        ('estimator', estimator)
    ])

# Print pipeline defined in a file
with open('A.txt', 'a') as f:
    print('Pipeline defined', file=f)

# create a logger object
logger = logging.getLogger('pipeline')
# set the level of logging
if args.debug:
    # enable debug logging if specified in the command line
    logger.setLevel(logging.DEBUG)
else:
    # otherwise use info logging
    logger.setLevel(logging.INFO)
# create a file handler object
file_handler = logging.FileHandler('log.txt', 'a')
# set the format of logging
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# add the formatter to the file handler
file_handler.setFormatter(formatter)
# add the file handler to the logger
logger.addHandler(file_handler)
# log the pipeline options
logger.info(f'Pipeline options: embedder={args.embedder}, fine_tune={args.fine_tune}, estimator={args.estimator}, train={args.train}, grid_search={args.grid_search}, param_grid={args.param_grid}, epochs={args.epochs}, steps={args.steps}, lr_embedder={args.lr_embedder}, lr_fine_tuned={args.lr_fine_tuned}, batch_size={args.batch_size}, device={args.device}, debug={args.debug}, quick={args.quick}, SP={args.SP}')

# load the data
INPUT_FOLDER = os.path.join('..', 'data', 'interim')
DATA_PATH = os.path.join(INPUT_FOLDER, '2_model_df.pkl')
splits = {
    'inner': 3,
    'outer': 3
}

X, y = load_data(DATA_PATH, args.quick, splits)
# log the data shape
logger.info(f'Data shape: X={X.shape}, y={y.shape}')

# perform nested cross-validation
param_grid = eval(args.param_grid) # convert the string to a dictionary
# if args.grid_search:
#     # perform grid search for the pipeline
#     logger.debug('Performing grid search for the pipeline')
#     grid = GridSearchCV(pipe, param_grid, cv=5)
#     scores = cross_val_score(grid, X, y, cv=5)
#     # log the best parameters and score from the grid search
#     if hasattr(grid, 'best_params_'):
#         best_params = grid.best_params_
#     else:
#         best_params = grid.best_estimator_.get_params()
#     logger.debug(f'Best parameters: {best_params}')
#     logger.debug(f'Best score: {grid.best_score_}')
# else:
#     # use the default parameters for the pipeline
#     logger.debug('Using the default parameters for the pipeline')
#     scores = cross_val_score(pipe, X, y, cv=5)

# # log the nested cross-validation scores and the mean score
# logger.info(f'Nested cross-validation scores: {scores}')
# logger.info(f'Mean score: {scores.mean()}')


if args.train:
    best_outer_score = float('-inf')  # Initialize with a very small value for maximization
    best_model = None
    best_params = None

    # Outer cross-validation
    outer_cv = StratifiedKFold(n_splits=splits['outer'], shuffle=True, random_state=42)
    outer_scores = []

    for fold, (train_index, test_index) in enumerate(outer_cv.split(X, y)):
        X_train, X_test = X[train_index], X[test_index]
        y_train, y_test = y[train_index], y[test_index]

        if args.grid_search:
            # Inner cross-validation for grid search
            inner_cv = StratifiedKFold(n_splits=splits['inner'], shuffle=True, random_state=42)
            grid = GridSearchCV(pipe, param_grid, cv=inner_cv)
            # Print train sets shapes on a file
            with open('A.txt', 'a') as f:
                print('X_train', X_train.shape, 'y_train', y_train.shape, file=f)

            grid.fit(X_train, y_train)

            # Evaluate the best model on the test set
            score = grid.score(X_test, y_test)
            outer_scores.append(score)

            # Log the best parameters and score from the grid search for this fold
            logger.debug(f'Best parameters for fold {fold+1}: {grid.best_params_}')
            logger.debug(f'Score for fold {fold+1}: {score}')

            # Check if this model has a better score than the current best
            if score > best_outer_score:
                best_outer_score = score
                best_model = grid.best_estimator_
                best_params = grid.best_params_

        else:
            # Fit the pipeline on the training data without grid search
            pipe.fit(X_train, y_train)

            # Evaluate the pipeline on the test set
            score = pipe.score(X_test, y_test)
            outer_scores.append(score)

            # Log the score for this fold
            logger.debug(f'Score for fold {fold+1}: {score}')

    # Save the best model
    if best_model is not None:
        # Create a name for the best model based on configuration and timestamp
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        model_name = f'best_model_{args.embedder}_{"fine_tune" if args.fine_tune else "no_fine_tune"}_{args.oversampling}_{args.estimator}_{timestamp}.pkl'
        joblib.dump(best_model, model_name)
        logger.debug(f'Best parameters across all folds: {best_params}')
        logger.debug(f'Best score across all folds: {best_outer_score}')
        logger.debug(f'Best model saved as: {model_name}')
    else:
        logger.warning('No best model found.')

    # Log the results of nested cross-validation
    logger.debug(f'Nested cross-validation scores: {outer_scores}')
    logger.debug(f'Mean score: {np.mean(outer_scores)}')

# Load model
# loaded_model = joblib.load('best_model.joblib')

# Make predictions with the loaded model
# predictions = loaded_model.predict(new_data)

if args.predict:
    loaded_model = joblib.load(args.predict)
    predictions = loaded_model.predict(X)
    # Save predictions in a file
    np.savetxt('predictions.txt', predictions, fmt='%s')

# Print end in a file
with open('A.txt', 'a') as f:
    print('End', file=f)