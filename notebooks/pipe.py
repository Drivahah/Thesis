# region Imports_______________________________________________________________________________________________________________________
import os
import argparse
import numpy as np
from sklearn.pipeline import Pipeline
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import GridSearchCV, StratifiedKFold
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import FunctionTransformer
from sklearn.metrics import (
    make_scorer,
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    roc_auc_score,
)
import joblib
import torch
from imblearn.pipeline import Pipeline as ImbPipeline
from imblearn.over_sampling import SMOTE, ADASYN
import logging
from datetime import datetime
from sklearn.model_selection import train_test_split
from torch.utils.data import random_split
import pickle
from sklearn.preprocessing import StandardScaler

# Set workiiing directory to file location
abspath = os.path.abspath(__file__)
dname = os.path.dirname(abspath)
os.chdir(dname)

# Import custom modules
import sys
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from torch.utils.data import random_split, DataLoader

sys.path.append("../src")
from PipelineFunctions import (
    load_data,
    flatten_data,
    ProtT5Embedder,
    ProtXLNetEmbedder,
    SequentialEmbedder,
    CustomRandomForestClassifier,
    plot_metrics,
    SklearnCompatibleAttentionClassifier,
    AttentionNetwork,
    CNNAttentionNetwork,
    ShapeLogger
)

# endregion _________________________________________________________________________________________________________________________

# region Argparse and logging setup_________________________________________________________________________________________________
# Parse the command line arguments
parser = argparse.ArgumentParser()
parser.add_argument(
    "--embedder",
    type=str,
    choices=["prott5", "protxlnet"],
    default="prott5",
    help="Embedder to use",
)
parser.add_argument(
    "--save_embeddings",
    action="store_true",
    help="Whether to save the embeddings to a file",
)
parser.add_argument(
    "--load_embeddings",
    action="store_true",
    help="Whether to load the embeddings from a file",
)
parser.add_argument(
    "--self_attention",
    action="store_true",
    help="Whether to use self-attention in the AttentionNetwork",
)
parser.add_argument(
    "--fine_tune", action="store_true", help="Whether to fine-tune the embedder"
)
parser.add_argument(
    "--oversampling",
    type=str,
    choices=["smote", "adasyn", "none"],
    default="none",
    help="Oversampling technique to use for imbalanced data",
)
parser.add_argument(
    "--classifier",
    type=str,
    choices=["rf", "crf", "attention"],
    default="logreg",
    help="Final classifier to use",
)
parser.add_argument(
    "--cnn",
    action="store_true",
    help="Whether to use a CNN in the AttentionNetwork",
)
parser.add_argument(
    "--train", action="store_true", help="Whether to train the whole model"
)
parser.add_argument(
    "--grid_search",
    action="store_true",
    help="Whether to perform grid search for the pipeline",
)
parser.add_argument(
    "--inner_splits",
    type=int,
    default=3,
    help="Number of splits for the inner cross-validation",
)
parser.add_argument(
    "--outer_splits",
    type=int,
    default=3,
    help="Number of splits for the outer cross-validation",
)
parser.add_argument(
    "--param_grid",
    type=str,
    default="{}",
    help="Parameter grid for the grid search as a string",
)
parser.add_argument(
    "--predict",
    type=str,
    default=None,
    help="Specify the pre-trained model to use to predict on the given data",
)
parser.add_argument(
    "--epochs",
    type=int,
    default=10,
    help="Number of epochs for fine-tuning the embedder",
)
parser.add_argument(
    "--steps",
    type=int,
    default=10,
    help="Number of steps to train the embedder before freezing the parameters",
)
parser.add_argument(
    "--batch_size", type=int, default=3, help="Batch size for embedding the data"
)
parser.add_argument(
    "--device", type=str, default="cuda:0", help="Device to use for the models"
)
parser.add_argument(
    "--debug", action="store_true", help="Whether to enable debug logging"
)
parser.add_argument("--quick", action="store_true", help="Run only on the first batch")
parser.add_argument(
    "--SP",
    type=str,
    choices=["parallel", "sequential"],
    default="sequential",
    help="Whether to embed the sequences in parallel or sequentially",
)
parser.add_argument("--logfile", type=str, default="log.txt", help="Log file name")
args = parser.parse_args()

# Define log file and add an empty line
LOG_FILENAME = os.path.join("..", "logs", args.logfile)
with open(LOG_FILENAME, "a") as f:
    f.write("\n")
# Create a logger object
logger = logging.getLogger("Pipeline")
if args.debug:
    logger.setLevel(logging.DEBUG)
else:
    logger.setLevel(logging.INFO)
file_handler = logging.FileHandler(LOG_FILENAME, "a")
formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.info("NEW RUN")

# Log the pipeline options
logger.info(
    f"Pipeline args: \
    embedder={args.embedder}, \
    fine_tune={args.fine_tune}, \
    classifier={args.classifier}, \
    train={args.train}, \
    grid_search={args.grid_search}, \
    param_grid={args.param_grid}, \
    predict={args.predict}, \
    epochs={args.epochs}, \
    steps={args.steps}, \
    batch_size={args.batch_size}, \
    device={args.device}, \
    debug={args.debug}, \
    quick={args.quick}, \
    SP={args.SP}"
)
# endregion _________________________________________________________________________________________________________________________

# region PIPELINE_______________________________________________________________________________________________________________________
n_jobs = -1  # -1 means use all processors during grid search
timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
MODELS_DIR = os.path.join("..", "models")
model_directory = os.path.join(MODELS_DIR, timestamp)
os.makedirs(model_directory)

scoring = {
    "accuracy": make_scorer(accuracy_score),
    "precision": make_scorer(precision_score),
    "recall": make_scorer(recall_score),
    "f1": make_scorer(f1_score),
    "roc_auc": make_scorer(roc_auc_score, needs_proba=True),
}  # `needs_proba=True` is for scorers that require probability outputs, like roc_auc
refit = "f1"

# Define the embedder
if args.classifier == "attention":
    prot = False
else:
    prot = True
if args.embedder == "prott5":
    emb_name = 'embeddings_T5'
    embedder_phage = ProtT5Embedder(
        "Rostlab/prot_t5_xl_half_uniref50-enc",
        fine_tune=args.fine_tune,
        device=args.device,
        debug=args.debug,
        prot=prot,
    )
    embedder_bacteria = ProtT5Embedder(
        "Rostlab/prot_t5_xl_half_uniref50-enc",
        fine_tune=args.fine_tune,
        device=args.device,
        org="bacteria",
        debug=args.debug,
        prot=prot,
    )
elif args.embedder == "protxlnet":
    emb_name = 'embeddings_XL'
    embedder_phage = ProtXLNetEmbedder(
        "Rostlab/prot_xlnet",
        fine_tune=args.fine_tune,
        device=args.device,
        debug=args.debug,
        prot=prot,
    )
    embedder_bacteria = ProtXLNetEmbedder(
        "Rostlab/prot_xlnet",
        fine_tune=args.fine_tune,
        device=args.device,
        org="bacteria",
        debug=args.debug,
        prot=prot,
    )

# Sequential or parallel embedder for phage and bacteria
if args.SP == "sequential":
    pair_embedder = SequentialEmbedder(embedder_phage, embedder_bacteria, prot=prot)
elif args.SP == "parallel":
    column_indices = {"sequence_phage": 0, "sequence_k12": 1}
    pair_embedder = ColumnTransformer(
        transformers=[
            ("embedder_phage", embedder_phage, [column_indices["sequence_phage"]]),
            ("embedder_bacteria", embedder_bacteria, [column_indices["sequence_k12"]]),
        ],
        remainder="drop",
    )  # drop any columns not specified in transformers

# Define oversampling technique
if args.oversampling == "smote":
    oversampling = SMOTE()
elif args.oversampling == "adasyn":
    oversampling = ADASYN()
else:
    oversampling = None

# Define the classifier
if args.classifier == "rf":
    classifier = RandomForestClassifier()
    if not args.grid_search:
        classifier.set_params(n_estimators=100, max_features="log2", min_samples_leaf=3, min_samples_split=2)
elif args.classifier == "crf":
    classifier = CustomRandomForestClassifier()
elif args.classifier == "attention":
    # TODO: if there will be any different length of a single sample, consider the various cases when defining input_dim
    input_dim = 1024
    if args.cnn:
        model = CNNAttentionNetwork(input_dim, self_attention=args.self_attention)
    else:
        model = AttentionNetwork(input_dim, self_attention=args.self_attention)
    classifier = SklearnCompatibleAttentionClassifier(
        model, model_directory, scoring=scoring, refit=refit
    )  # TODO: add lr, batch_size and epochs____________________________________________________________
    n_jobs = 1  # AttentionNetwork is not picklable, so n_jobs must be 1

    # Define the pipeline
if oversampling is not None:
    pipe = ImbPipeline([("oversampling", oversampling),
                        ("shape_logger", ShapeLogger("oversampling")),
                        ("classifier", classifier),])
else:
    pipe = Pipeline([("classifier", classifier)])
# endregion _________________________________________________________________________________________________________________________

# region Load data and embed________________________________________________________________________________________________________
# Load data
INPUT_FOLDER = os.path.join("..", "data", "interim")
DATA_PATH = os.path.join(INPUT_FOLDER, "2_model_df.pkl")
X, y = load_data(DATA_PATH, args.quick, args.debug)
logger.info(f"LOAD DATA\nData shape: X={X.shape}, y={y.shape}")
logger.debug(f"X[:5]:\n {X[:5]}\ny[:5]:\n {y[:5]}")

# Embed data
logger.debug("EMBED DATA")
EMB_FILE = f'{emb_name}_prot.pt' if prot else f'{emb_name}_res.pt'
if args.load_embeddings:
    try:
        if os.path.exists(os.path.join(INPUT_FOLDER, EMB_FILE)):
            logger.info(f"Loading embeddings from file: {EMB_FILE}")
            X = torch.load(os.path.join(INPUT_FOLDER, EMB_FILE))
            logger.info(f"Embeddings loaded from file: {EMB_FILE}")
        else:
            X = pair_embedder.transform(X, batch_size=args.batch_size)
    except Exception as e:
        logger.error(f"Error while loading embeddings: {e}")
        sys.exit(1)
else:
    logger.info("Embedding data")
    X = pair_embedder.transform(X, batch_size=args.batch_size)
    if prot:
        logger.debug(
            f"FINISHED EMBEDDING:\nData shape after embedding: X={X.shape}, y={y.shape}\nX[:5]:\n {X[:5]}\ny[:5]:\n {y[:5]}"
        )
    else:
        logger.debug('FINISHED EMBEDDING')
if args.save_embeddings:
    try:
        logger.info("Saving embeddings to file")
        torch.save(X, os.path.join(INPUT_FOLDER, EMB_FILE))
    except Exception as e:
        logger.error(f"Error while saving embeddings: {e}")
# endregion _________________________________________________________________________________________________________________________

# region Grid search and nested cross-validation ___________________________________________________________________________________

outer_predictions = dict()

# (Nested) cross-validation
param_grid = eval(args.param_grid)  # convert the string to a dictionary
if not os.path.exists(MODELS_DIR):
    os.makedirs(MODELS_DIR)
if args.train:
    if args.cnn:
        # Split X in train and test dataset
        max_size = max(arr.shape[0] for arr in X)
        X = [np.pad(arr, ((0, max_size - arr.shape[0]), (0, 0)), mode='constant', constant_values=0) for arr in X]  # Pad arrays to same size, so that they can be converted to a tensor

        # Split dataset into training and testing sets
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

        # Normalize the data
        X_train = np.array(X_train)
        X_test = np.array(X_test)
        X_train_reshaped = X_train.reshape(-1, X_train.shape[-1])
        X_test_reshaped = X_test.reshape(-1, X_test.shape[-1])

        scaler = StandardScaler()
        X_train_normalized = scaler.fit_transform(X_train_reshaped)
        X_test_normalized = scaler.transform(X_test_reshaped)

        X_train_normalized = X_train_normalized.reshape(X_train.shape)
        X_test_normalized = X_test_normalized.reshape(X_test.shape)

        # Convert numpy arrays to tensors
        X_train_tensor = torch.tensor(X_train_normalized, dtype=torch.float32)
        X_test_tensor = torch.tensor(X_test_normalized, dtype=torch.float32)
        y_train_tensor = torch.tensor(y_train, dtype=torch.float32)
        y_test_tensor = torch.tensor(y_test, dtype=torch.float32)

        # Split training dataset into training and validation sets
        train_dataset = torch.utils.data.TensorDataset(X_train_tensor, y_train_tensor)
        train_size = int(0.8 * len(train_dataset))
        val_size = len(train_dataset) - train_size
        train_dataset, val_dataset = random_split(train_dataset, [train_size, val_size])

        # Define dataloaders
        train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True)
        val_loader = DataLoader(val_dataset, batch_size=32, shuffle=False)
        test_dataset = torch.utils.data.TensorDataset(X_test_tensor, y_test_tensor)
        test_loader = DataLoader(test_dataset, batch_size=32, shuffle=False)

        # Initialize the model, loss function, and optimizer
        model = CNNAttentionNetwork(input_dim, self_attention=args.self_attention)

        # Define the class weights for the positive and negative classes
        weight_negative = 1.0  # Weight for the negative class
        weight_positive = 10.0  # Weight for the positive class

        # Instantiate the BCE loss function with class weights
        class_weights = torch.tensor([weight_negative, weight_positive])
        criterion = torch.nn.BCELoss(weight=class_weights)
        optimizer = torch.optim.Adam(model.parameters(), lr=0.001)


        def train(model, train_loader, val_loader, criterion, optimizer, num_epochs=10, patience=3):
            train_loss = []  # List to store the training loss
            val_f1_scores = []  # List to store the validation F1 scores
            best_val_f1 = -np.inf
            early_stopping_counter = 0

            for epoch in range(num_epochs):
                model.train()
                running_loss = 0.0
                for inputs, labels in train_loader:
                    optimizer.zero_grad()
                    outputs = model(inputs)
                    logger.info(f"Outputs: {outputs}")
                    logger.info(f"Labels: {labels}")
                    labels_onehot = torch.zeros(labels.size(0), outputs.size(1))
                    labels_onehot.scatter_(1, labels.view(-1, 1).long(), 1)
                    labels_onehot = labels_onehot.to(outputs.device)  # Move labels tensor to the same device as outputs
                    loss = criterion(outputs, labels_onehot)
                    loss.backward()
                    optimizer.step()
                    running_loss += loss.item() * inputs.size(0)

                # Validate the model
                model.eval()
                val_predictions = []
                val_targets = []
                for inputs, labels in val_loader:
                    outputs = model(inputs)
                    _, predictions = torch.max(outputs, 1)
                    val_predictions.extend(predictions.tolist())
                    val_targets.extend(labels.tolist())

                val_f1 = f1_score(val_targets, val_predictions)
                logger.info(f"Epoch {epoch+1}/{num_epochs}, Loss: {running_loss/len(train_loader.dataset)}, Validation F1: {val_f1}")

                train_loss.append(running_loss/len(train_loader.dataset))  # Append the training loss to the list
                val_f1_scores.append(val_f1)  # Append the validation F1 score to the list

                if val_f1 > best_val_f1:
                    best_val_f1 = val_f1
                    early_stopping_counter = 0
                else:
                    early_stopping_counter += 1
                    if early_stopping_counter >= patience:
                        logger.info(f"Early stopping at epoch {epoch+1}")
                        break

            # Save the training loss and validation F1 scores as pickle files
            with open(os.path.join(model_directory, 'train_loss.pkl'), 'wb') as f:
                pickle.dump(train_loss, f)
            with open(os.path.join(model_directory, 'val_f1_scores.pkl'), 'wb') as f:
                pickle.dump(val_f1_scores, f)

        # test the model
        def test(model, test_loader):
            model.eval()
            test_predictions = []
            test_targets = []
            for inputs, labels in test_loader:
                outputs = model(inputs)
                _, predictions = torch.max(outputs, 1)
                test_predictions.extend(predictions.tolist())
                test_targets.extend(labels.tolist())

            test_f1 = f1_score(test_targets, test_predictions)
            logger.info(f"Test F1: {test_f1}")

            # Save test predictions and targets as pickle files
            with open(os.path.join(model_directory, 'test_predictions.pkl'), 'wb') as f:
                pickle.dump(test_predictions, f)
            with open(os.path.join(model_directory, 'test_targets.pkl'), 'wb') as f:
                pickle.dump(test_targets, f)

        train(model, train_loader, val_loader, criterion, optimizer, num_epochs=args.epochs)
        test(model, test_loader)
    else:
        logger.info("TRAINING THE WHOLE MODEL")
        logger.debug(f'Inner splits: {args.inner_splits}, outer splits: {args.outer_splits}')
        best_outer_score = float(
            "-inf"
        )  # Initialize with a very small value for maximization
        best_model = None
        best_params = None

        # Outer cross-validation
        outer_scores = []
        outer_cv = StratifiedKFold(n_splits=args.outer_splits, shuffle=True, random_state=42)
        cv_results_dict = dict()
        best_dict = dict()
        for fold, (train_index, test_index) in enumerate(outer_cv.split(X, y)):
            logger.debug(f"Outer fold {fold+1}")
            if prot:
                X_train, X_test = X[train_index], X[test_index]
            else:
                X_train, X_test = [X[i] for i in train_index], [X[i] for i in test_index]
                max_size = max(max(arr.shape[0] for arr in X_train), max(arr.shape[0] for arr in X_test))
                X_train = [np.pad(arr, ((0, max_size - arr.shape[0]), (0, 0)), mode='constant', constant_values=0) for arr in X_train]  # Pad arrays to same size, so that they can be converted to a tensor
                X_test = [np.pad(arr, ((0, max_size - arr.shape[0]), (0, 0)), mode='constant', constant_values=0) for arr in X_test]
                X_train = np.array(X_train)
                X_test = np.array(X_test)

            y_train, y_test = y[train_index], y[test_index]
            logger.info(
                f"X_train[:5]:\n {X_train[:5]}\ny_train[:5]:\n {y_train[:5]}\nX_test[:5]:\n {X_test[:5]}\ny_test[:5]:\n {y_test[:5]}"
            )

            # Grid search and inner CV
            if args.grid_search:
                """
                Inside grid.fit, all the hyperparameters combinations are tested.
                For each combination, a stratified CV is performed on the training set.
                Stratified means that the proportion of the classes is preserved in each fold.
                The combination with the best score (folds average) is selected.
                The selected combination of hyperparameters is then used to fit the pipeline on the whole training set.
                This model is evaluated on the test set (i.e. 1 fold of the outer CV).
                """
                logger.debug("PERFORMING GRID SEARCH")
                inner_cv = StratifiedKFold(n_splits=args.inner_splits, shuffle=True, random_state=42)
                grid = GridSearchCV(
                    pipe,
                    param_grid,
                    cv=inner_cv,
                    scoring=scoring,
                    refit=refit,
                    verbose=3,
                    n_jobs=n_jobs,
                )
                grid.fit(X_train, y_train)

                # Save the results of the grid search
                cv_results_dict[f"fold_{fold}"] = grid.cv_results_
                best_dict[f"fold_{fold}"] = dict()
                best_dict[f"fold_{fold}"]["best_score"] = grid.best_score_
                best_dict[f"fold_{fold}"]["best_params"] = grid.best_params_
                best_dict[f"fold_{fold}"]["best_index"] = grid.best_index_  # index of the best combination of hyperparameters
                
                logger.info(f"Best parameters for fold {fold}: {grid.best_params_}")
                logger.info(f"Best {refit} score for fold {fold}: {grid.best_score_}")

                outer_predictions[f"fold_{fold}"] = dict()
                outer_predictions[f"fold_{fold}"]['y_test'] = y_test
                outer_predictions[f"fold_{fold}"]['y_proba'] = grid.predict_proba(X_test)
                
                # The best parameters are used to fit a new model - best_estimator_ - on the training set of the outer fold
                # This model is evaluated on the test set of the outer fold, returning grid.score
                if args.cnn:
                    outer_predictions[f"fold_{fold}"]['y_proba'] = grid.predict_proba(X_test)
                    y_pred = grid.predict(X_test)
                    outer_score = f1_score(y_test, y_pred)
                else:
                    outer_score = grid.score(X_test, y_test) 
                logger.info(f"Outer {refit} score for fold {fold} using the model trained on outer training with the best parameters: {outer_score}")
                
            # Fit the pipeline on the training data without grid search and inner CV
            else:
                pipe.fit(X_train, y_train)
                outer_score = pipe.score(X_test, y_test)
                outer_predictions[f"fold_{fold}"] = dict()
                outer_predictions[f"fold_{fold}"]['y_test'] = y_test
                outer_predictions[f"fold_{fold}"]['y_proba'] = pipe.predict_proba(X_test)

            outer_scores.append(outer_score)

            # Update best model
            if outer_score > best_outer_score:
                best_outer_score = outer_score
                if args.grid_search:
                    best_model = grid.best_estimator_
                    best_outer_params = grid.best_params_

        best_dict["best_outer_score"] = best_outer_score
        best_dict["outer_scores"] = outer_scores
        best_dict["mean_outer_score"] = np.mean(outer_scores)
        if args.grid_search:
            best_dict["best_outer_params"] = best_outer_params

        # Save the best model
        if best_model is not None:
            
            model_name = f"Model_{timestamp}.pkl"
            joblib.dump(best_model, os.path.join(model_directory, model_name))
            # Save the results of the grid search as json files
            if args.grid_search:
                logger.info(f"Saving grid search results in {model_directory}")
                joblib.dump(cv_results_dict, os.path.join(model_directory, "cv_results.pkl"))
                joblib.dump(best_dict, os.path.join(model_directory, "best_dict.pkl"))
                joblib.dump(outer_predictions, os.path.join(model_directory, "outer_predictions.pkl"))
            
                logger.info(f"Best parameters across all outer folds: {best_outer_params}")
                # Save the best parameters to a file
                with open("best_parameters.txt", "a") as f:
                    f.write(f"Model: {model_name}\n")
                    f.write(f"Embedder: {args.embedder}\n")
                    f.write(f"Oversampling: {args.oversampling}\n")
                    f.write(f"classifier: {args.classifier}\n")
                    f.write(f"Hyperparameters: {best_outer_params}\n")
                    f.write(f"Outer score achieved by the model: {best_outer_score}\n")
                    f.write(f"All outer scores: {outer_scores}\n")
                    f.write("Outer splits: " + str(args.outer_splits) + "\n")
                    if args.grid_search:
                        f.write(f'Inner splits: {str(args.inner_splits)}\n')
                    f.write("\n")
            else:
                logger.info(f"Best model saved as: {model_name}")
                joblib.dump(outer_predictions, os.path.join(model_directory, "outer_predictions.pkl"))
                joblib.dump(outer_scores, os.path.join(model_directory, "best_dict.pkl"))

                with open("best_parameters.txt", "a") as f:
                    f.write(f"Model: {model_name}\n")
                    f.write(f"Embedder: {args.embedder}\n")
                    f.write(f"Oversampling: {args.oversampling}\n")
                    f.write(f"classifier: {args.classifier}\n")
                    f.write(f"Outer score achieved by the model: {best_outer_score}\n")
                    f.write(f"All outer scores: {outer_scores}\n")
                    f.write("Outer splits: " + str(args.outer_splits) + "\n")
                    f.write("\n")

            logger.info(f"Best score across all outer folds: {best_outer_score}")
            logger.info(f"Best model saved as: {model_name}")


        else:
            logger.warning("No best model found.")

        # Log the results of nested cross-validation
        logger.info(
            f"Nested cross-validation scores: {outer_scores}\nMean score: {np.mean(outer_scores)}"
        )

PREDICTIONS_DIR = os.path.join("..", "data", "predictions")
if not os.path.exists(PREDICTIONS_DIR):
    os.makedirs(PREDICTIONS_DIR)
if args.predict:
    loaded_model = joblib.load(os.path.join(MODELS_DIR, args.predict))
    predictions = loaded_model.predict(X)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    predictions_file = f"predictions_{args.predict}_{timestamp}.txt"
    np.savetxt(os.path.join(PREDICTIONS_DIR, predictions_file), predictions, fmt="%s")

# endregion _________________________________________________________________________________________________________________________
