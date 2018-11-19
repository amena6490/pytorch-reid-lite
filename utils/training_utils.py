from __future__ import division

import time
import logging
import torch
import random
import torch.nn as nn

from model_utils import save_and_evaluate
from nets import layers


def _get_xent_loss(config, criterion, outputs, labels, step=None):
    loss = criterion(outputs, labels).mean() \
        if config["model_params"].get("pcb_n_parts", 0) == 0 \
        else sum([criterion(output, labels) for output in outputs])
    return loss


def _compute_batch_acc(config, outputs, labels, step=None, class_balanced=False):
    batch_params = config["batch_sampling_params"]
    #batch_size = config["batch_size"] \
    #    if not class_balanced \
    #    else batch_params["P"] * batch_params["K"]
    if config["model_params"].get("pcb_n_parts", 0) == 0:
        batch_size = outputs.shape[0]
    else:
        batch_size = outputs[0].shape[0]
    _, preds = torch.max(outputs.data, 1) \
        if config["model_params"].get("pcb_n_parts", 0) == 0 \
        else torch.max(torch.mean(torch.stack(outputs), dim=0).data, 1)
    batch_acc = torch.sum(preds == labels).item() / batch_size

    return batch_acc


def _get_loss_d(img, netD, label_cls, criterion):
    b_size = img.shape[0]
    output = netD(img)
    output = output.view(-1)
    errD_cls = criterion(output, label_cls)
    D_x = output.mean().item()
    return errD_cls, D_x


def get_loss_reid(img, label, cls_net, loss, config):
    pred_id_REID = cls_net(img, label)
    if config["model_params"].get("pcb_n_parts", 0) == 0:
        errG_id_REID = loss(pred_id_REID, label)
    else:
        errG_id_REID = sum([loss(output, label) for output in
            pred_id_REID])
    accG_REID = _compute_batch_acc(config, pred_id_REID, label)
    return errG_id_REID, accG_REID


def run_iter_gan(images, labels, step, epoch, config, netG, netD,
                    loss, optimizerD, optimizerG, net=None, loss_dict=None):
    real_label = 1
    fake_label = 0
    b_size = images.size(0)

    gan_params = config["gan_params"]
    nz = gan_params["input_dim"]
    d_update_freq = gan_params.get("d_update_freq", 1)
    update_d = (random.random() < d_update_freq)
    update_adv = (random.random() < 0.2)
    netD.zero_grad()
    label_cls = torch.full((b_size,), real_label, dtype=torch.float).cuda()

    #update D with real images 
    errD_real_cls, D_x = _get_loss_d(images, netD, label_cls, loss)
    if update_d:
        errD_real_cls.backward(retain_graph=False)
    
    #updge D with fake images
    noise = torch.randn(b_size, nz).cuda()
    noise_norm = noise.pow(2).sum(1).pow(0.5).view(-1, 1)
    noise = noise / noise_norm.view(-1, 1)
    label_cls.fill_(fake_label)
    fake = netG(noise)
    errD_fake_cls, D_G_z1 = _get_loss_d(fake.detach(), netD, label_cls, loss)
    if update_d:
        errD_fake_cls.backward(retain_graph=True)
        optimizerD.step()

    #updage G
    netG.zero_grad()
    netD.zero_grad()
    label_cls.fill_(real_label)
    errG_cls, D_G_z2 = _get_loss_d(fake, netD, label_cls, loss) 
    if gan_params["adv_train"] and net is not None and \
            step % 50 == 0:
        feature = net(images, return_feature=True).data
        noise_f = feature + torch.randn_like(feature) * 0.1
        noise_f_norm = noise_f.pow(2).sum(1).pow(0.5).view(-1, 1)
        noise_f = noise_f / noise_f_norm.view(-1, 1)
        fake_f = netG(noise_f) 
        errG_cls_f, _ = _get_loss_d(fake_f, netD, label_cls, loss)
        errG_id_REID, accG_REID = get_loss_reid(fake_f, labels, net,
                loss_dict["xent_loss"], config)
        errG_id_REID.backward(retain_graph=True)
        errG_cls_f.backward(retain_graph=True)
        if step > 0:
            logging.info("epoch [%.3d] iter = %d errG_id_REID:%.3f, accG_REID:%.3f"
                    %(epoch, step, errG_id_REID.item(), accG_REID))
    errG_cls.backward()
    optimizerG.step()

    log_step = 50 if config.get("model_parallel", False) else 50
    if step > 0 and step % log_step == 0:
        logging.info(
                "epoch [%.3d] iter = %d loss_r = %.3f loss_f = %.3f"
                " loss_g = %.3f D_r = %.3f D_f = %.3f|%.3f" %
            (epoch, step, errD_real_cls.item(), errD_fake_cls.item(),
                errG_cls.item(), D_x, D_G_z1, D_G_z2)
            )

     


def run_iter_adv(images, labels, features, step, epoch, config, net, loss_dict,
                     optimizer, netG, netD, loss_gan,
                     optimizerD, optimizerG):
    real_label = 1
    fake_label = 0
    b_size = images.size(0)

    update_adv = (random.random() < 0.1)

    gan_params = config["gan_params"]
    nz = gan_params["input_dim"]
    d_update_freq = gan_params.get("d_update_freq", 1)
    update_d = (random.random() < d_update_freq)
    netD.zero_grad()
    label_cls = torch.full((b_size,), real_label, dtype=torch.float).cuda()

    #update D with real images 
    errD_real_cls, D_x = _get_loss_d(images, netD, label_cls, loss_gan)
    errD_real_cls.backward(retain_graph=False)
    
    #updge D with fake images
    noise = features.detach().cuda()
    noise_norm = noise.pow(2).sum(1).pow(0.5).view(-1, 1)
    noise = noise / noise_norm.view(-1, 1)
    label_cls.fill_(fake_label)
    fake = netG(noise)
    errD_fake_cls, D_G_z1 = _get_loss_d(fake.detach(), netD, label_cls,
            loss_gan)
    errD_fake_cls.backward(retain_graph=True)
    optimizerD.step()

    #updage G
    loss_gid = loss_dict["xent_loss"]
    netG.zero_grad()
    netD.zero_grad()
    label_cls.fill_(real_label)
    errG_cls, D_G_z2 = _get_loss_d(fake, netD, label_cls, loss_gan) 
    errG_id_REID, accG_REID = get_loss_reid(fake, labels, net, loss_gid, config)
    if update_adv:
        errG_id_REID.backward(retain_graph=True)
    errG_cls.backward(retain_graph=True)
    optimizerG.step()

    # learn from adversarial sample 
    batch_acc = 0
    if update_adv: 
        net.zero_grad()
        labels_shift = labels + config["num_labels"]
        outputs = net(fake.detach(), labels=labels_shift, return_feature=False)
        loss_id = _get_xent_loss(config, loss_gid, outputs, labels_shift)
        batch_acc = _compute_batch_acc(config, outputs, labels_shift)
        loss_id.backward()
        optimizer.step()

    #loss_id.backward()
    if update_adv:
        logging.info("loss_r:%.3f loss_f = %.3f loss_g = %.3f D_r = %.3f "
                "D_f = %.3f|%.3f G_att_acc:%.3f, att_loss:%.3f, adv_acc:%3f" % 
                (errD_real_cls.item(), errD_fake_cls.item(),errG_cls.item(),
                    D_x, D_G_z1, D_G_z2, accG_REID, errG_id_REID.item(),
                    batch_acc))
    
    """
    optimizer.zero_grad()
    real_label = 1
    fake_label = 0
    b_size = images.size(0)
    loss_gid = loss_dict["xent_loss"]

    gan_params = config["gan_params"]
    nz = gan_params["input_dim"]
    netD.zero_grad()
    label_cls = torch.full((b_size,), real_label, dtype=torch.float).cuda()

    #update D with real images 
    errD_real_cls, D_x = _get_loss_d(images, netD, label_cls, loss_gan)
    errD_real_cls.backward(retain_graph=False)
    
    #updge D with fake images
    noise = features.cuda()
    noise_norm = noise.pow(2).sum(1).pow(0.5).view(-1, 1)
    noise = noise / noise_norm.view(-1, 1)
    label_cls.fill_(fake_label)
    fake = netG(noise)
    errD_fake_cls, D_G_z1 = _get_loss_d(fake.detach(), netD, label_cls, loss_gan)
    errD_fake_cls.backward(retain_graph=True)
    optimizerD.step()
    
    #updage G
    netG.zero_grad()
    netD.zero_grad()
    label_cls.fill_(real_label)
    errG_cls, D_G_z2 = _get_loss_d(fake, netD, label_cls, loss_gan) 
    errG_cls.backward(retain_graph=True)
    optimizerG.step()
    errG_id_REID, accG_REID = get_loss_reid(fake, labels, net, loss_gid, config)
    errG_id_REID.backward(retain_graph=True)

    optimizerG.step()
     
    # Forward and backward
    labels_shift = labels + config["num_labels"]
    outputs = net(fake.detach(), labels=labels_shift, return_feature=False)
    criterion = loss_dict["xent_loss"]
    loss_id = _get_xent_loss(config, criterion, outputs, labels_shift)
    batch_acc = _compute_batch_acc(config, outputs, labels_shift)

    loss_id.backward()
    optimizer.step()
    """


def run_iter_softmax(images, labels, step, epoch, config, net, loss_dict,
                     optimizer, evaluate_func, iter_start_time,
                     io_finished_time, unlabel_buffer=None, **kwargs):
    # Forward and backward
    optimizer.zero_grad()
    feature = None
    feature, outputs = net(images, labels=labels, return_feature=True)
    if unlabel_buffer is not None:
       logit_un = feature.mm(unlabel_buffer)
       outputs = torch.cat([outputs, logit_un], 1)
    criterion = loss_dict["xent_loss"]
    loss = _get_xent_loss(config, criterion, outputs, labels, step)

    loss.backward()
    optimizer.step()

    log_step = 50 if config.get("model_parallel", False) else 50
    if step > 0 and step % log_step == 0:
        step_finished_time = time.time()
        gpu_time = float(step_finished_time - io_finished_time)
        io_time = float(io_finished_time - iter_start_time)
        example_per_second = config["batch_size"] / (gpu_time + io_time)
        io_percentage = io_time / (gpu_time + io_time)
        batch_acc = _compute_batch_acc(config, outputs, labels, step)

        logging.info(
            "epoch [%.3d] iter = %d loss = %.4f acc = %.5f example/sec = %.3f, "
            "io_percentage = %.3f" %
            (epoch, step, loss.item(), batch_acc, example_per_second,
             io_percentage)
        )

        # Write summary
        config["tensorboard_writer"].add_scalar("loss",
                                                loss.item(),
                                                config["global_step"])

        config["tensorboard_writer"].add_scalar("batch_accuray",
                                                batch_acc,
                                                config["global_step"])

    if step > 0 and step % 1000 == 0:
        save_and_evaluate(net, config, None)

    if evaluate_func and step > 0 \
            and config["evaluation_params"].get("step", None) \
            and step % config["evaluation_params"]["step"] == 0:
        save_and_evaluate(net, config, evaluate_func, save_ckpt=False)
    
    return feature


def run_iter_triplet_loss(images, labels, config, net, loss_dict,
                          optimizer, evaluate_func, iter_start_time,
                          io_finished_time, **kwargs):
    config["global_step"] += 1
    step = config["global_step"]
    tri_loss = loss_dict["tri_loss"]

    # Forward and backward
    optimizer.zero_grad()
    if config["tri_loss_params"]["lambda_cls"]:
        # Joint training loss
        outputs = net(images, labels=labels)
        loss_tri, pull_ratio, active_triplet,\
            mean_dist_an, mean_dist_ap,\
            = tri_loss(outputs[0], labels, step)
        loss_cls = _get_xent_loss(config, loss_dict["xent_loss"], outputs[1],
                                  labels, step)
        loss = loss_cls * config["tri_loss_params"]["lambda_cls"] + \
            loss_tri * config["tri_loss_params"]["lambda_tri"]
    else:
        outputs = net(images, labels=labels)
        loss, pull_ratio, active_triplet,\
            mean_dist_an, mean_dist_ap \
            = tri_loss(outputs, labels, step)

    loss.backward()
    optimizer.step()

    if step > 0 and step % 10 == 0:
        step_finished_time = time.time()
        gpu_time = float(step_finished_time - io_finished_time)
        io_time = float(io_finished_time - iter_start_time)
        example_per_second = config["batch_size"] / (io_time + gpu_time)
        io_percentage = io_time / (gpu_time + io_time)
        if config["tri_loss_params"]["lambda_cls"]:
            logging.info(
                "global_step = %d tri_loss = %.4f xent_loss = %.4f "
                "example/sec = %.3f, io_percentage = %.3f" %
                (step, loss_tri.item(), loss_cls.item(),
                 example_per_second, io_percentage)
            )
        else:
            logging.info(
                "global_step = %d loss = %.4f example/sec = %.3f, "
                "io_percentage = %.3f" %
                (step, loss.item(),  example_per_second, io_percentage)
            )

        # Writer summaries
        config["tensorboard_writer"].add_scalar(
            "loss", loss.item(), config["global_step"])

        config["tensorboard_writer"].add_scalar(
            "AN_lt_AP_ratio", pull_ratio.item(), config["global_step"])

        config["tensorboard_writer"].add_scalar(
            "Active_Triplet", active_triplet.item(), config["global_step"])

        config["tensorboard_writer"].add_scalar(
            "Mean_Dist_Difference", mean_dist_an.item() - mean_dist_ap.item(),
            config["global_step"])

    # log triplet loss info and acc
    if step > 0 and step % 100 == 0:
        logging.info("[TRI_LOSS_INFO] AN > AP: %.2f%%; ACTIVE_TRIPLET: %d;"
                     " MEAN_DIST_AN: %.2f; MEAN_DIST_AP: %.2f" %
                     (pull_ratio.item(), active_triplet.item(),
                      mean_dist_an.item(), mean_dist_ap.item()))

        # log acc if training with joint loss
        if config["tri_loss_params"]["lambda_cls"]:
            batch_acc = _compute_batch_acc(config, outputs[1], labels, step,
                                           class_balanced=True)
            logging.info("train_accuracy: %.5f" % batch_acc)

    if step > 0 and step % 1000 == 0:
        save_and_evaluate(net, config, None)

    if evaluate_func and config["evaluation_params"].get("step", None)\
            and step % config["evaluation_params"]["step"] == 0:
        save_and_evaluate(net, config, evaluate_func, save_ckpt=False)


def get_loss_dict(config):
    use_tri_loss = config["tri_loss_params"]["margin"] > 0
    loss_dict = {}
    if use_tri_loss:
        logging.info("Using Triplet Loss: %s" % config["tri_loss_params"])
        loss_dict["tri_loss"] = layers.TripletLoss(
            config["tri_loss_params"]["margin"],
            config["tri_loss_params"]["use_adaptive_weight"]
        )

    if not use_tri_loss \
            or (use_tri_loss and config["tri_loss_params"]["lambda_cls"] > 0):
        loss_dict["xent_loss"] = nn.CrossEntropyLoss()

    return loss_dict
